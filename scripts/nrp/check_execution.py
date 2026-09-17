#!/usr/bin/env python3
"""Check completed traces, CPU/GPU selection, publication, and cache-only resumes."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shlex
import ssl
import urllib.request

ROOT = Path(os.environ["TEST_ROOT"])
EXPECTED = {"job_dispatch", "preprocessing", "spikesort_kilosort4", "postprocessing",
            "curation", "visualization", "results_collector", "nwb_ecephys", "nwb_units",
            "report_generation", "burst_detection"}
IMAGES = json.loads(Path(__file__).with_name("images.json").read_text())
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TLS = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--workflow-device", choices=("cpu", "gpu"),
                    help="Check one finished workflow while the other is still running")
options = parser.parse_args()


def task_folder(run, short_hash):
    prefix, suffix = short_hash.split("/")
    matches = list((run / "work" / prefix).glob(suffix + "*"))
    assert len(matches) == 1, (short_hash, matches)
    return matches[0]


def same_file(source, published):
    assert source.is_file() and published.is_file(), (source, published)
    assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(published.read_bytes()).digest(), \
        f"Published artifact differs from the latest task: {published}"


def check_resources(name, row, device):
    url = (f"https://kubernetes.default.svc/apis/batch/v1/namespaces/"
           f"{os.environ['TEST_NAMESPACE']}/jobs/{row['native_id']}")
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + (SERVICE_ACCOUNT / "token").read_text().strip()})
    with urllib.request.urlopen(request, context=TLS, timeout=30) as response:
        job = json.load(response)
    assert job["metadata"]["labels"]["mea-test"] == os.environ["TEST_ID"]
    pod = job["spec"]["template"]["spec"]
    assert job["spec"]["backoffLimit"] == 0 and pod["restartPolicy"] == "Never"
    assert pod["activeDeadlineSeconds"] > 0 and pod["automountServiceAccountToken"] is False
    container = pod["containers"][0]
    resources = container["resources"]
    assert resources["requests"] == resources["limits"]
    assert container["image"] == row["container"]
    assert resources["requests"]["cpu"] == row["cpus"]
    expected_gpu = "1" if device == "gpu" and name == "spikesort_kilosort4" else "0"
    assert resources["requests"].get("nvidia.com/gpu", "0") == expected_gpu
    if name == "spikesort_kilosort4":
        assert resources["requests"]["memory"] == "4Gi"
    return {"job": row["native_id"], **resources["requests"]}


result = {}
for device in ((options.workflow_device,) if options.workflow_device else ("cpu", "gpu")):
    run = ROOT / "runs" / device
    successful = []
    for path in sorted((run / "evidence").glob("exit-*.json")):
        stamp = path.stem.removeprefix("exit-")
        command = json.loads((run / "evidence" / f"command-{stamp}.json").read_text())
        if json.loads(path.read_text())["exit_code"] == 0 and "-preview" not in command:
            with (run / "evidence" / f"trace-{stamp}.tsv").open() as file:
                rows = list(csv.DictReader(file, delimiter="\t"))
            tasks = {row["name"].split(" (")[0]: row for row in rows}
            assert len(rows) == len(EXPECTED) and set(tasks) == EXPECTED
            assert all(r["status"] in ("COMPLETED", "CACHED") and r["exit"] == "0" for r in rows)
            successful.append((stamp, command, tasks))
    assert len(successful) >= 2, f"Missing completed run and resume for {device}"
    stamp, command, tasks = successful[-1]
    previous = successful[-2][2]
    assert "-resume" in command and all(row["status"] == "CACHED" for row in tasks.values())
    assert {k: r["hash"] for k, r in tasks.items()} == {k: r["hash"] for k, r in previous.items()}
    resources = {}
    for name, row in tasks.items():
        image = "ks4" if name == "spikesort_kilosort4" else "nwb" if name.startswith("nwb_") else "base"
        assert row["container"] == IMAGES[image], (name, row["container"])
        resources[name] = check_resources(name, row, device)
    sorter = task_folder(run, tasks["spikesort_kilosort4"]["hash"])
    line = next(line for line in (sorter / ".command.sh").read_text().splitlines()
                if line.strip().startswith("./run --params "))
    args = shlex.split(line)
    settings = json.loads(args[args.index("--params") + 1])
    assert settings["sorter"]["torch_device"] == ("cuda" if device == "gpu" else "cpu")
    for stage, subdir, filename in (("report_generation", "reports", "report_summary.json"),
                                    ("burst_detection", "bursts", "network_results.json")):
        work = task_folder(run, tasks[stage]["hash"])
        sources = list((work / "capsule/results").glob(f"*/{filename}"))
        assert len(sources) == 1
        same_file(sources[0], run / "results" / subdir / sources[0].parent.name / filename)
    work = task_folder(run, tasks["nwb_units"]["hash"])
    sources = list((work / "capsule/results").glob("*.nwb*/.zmetadata"))
    assert len(sources) == 1, "Expected the fixture's Zarr NWB output"
    same_file(sources[0], run / "results/nwb" / sources[0].parent.name / ".zmetadata")
    result[device] = {"resume_stamp": stamp, "cached_tasks": len(tasks),
                      "hashes": {name: row["hash"] for name, row in tasks.items()},
                      "sorter_device": settings["sorter"]["torch_device"],
                      "resources": resources,
                      "publication_matches_latest_tasks": True}

if len(result) == 2:
    assert result["cpu"]["hashes"]["spikesort_kilosort4"] != result["gpu"]["hashes"]["spikesort_kilosort4"]
filename = f"execution-{options.workflow_device}.json" if options.workflow_device else "execution.json"
(ROOT / "validation" / filename).write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
