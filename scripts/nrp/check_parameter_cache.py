#!/usr/bin/env python3
"""Exercise parameter/pin changes across real Nextflow cache/resume cycles.

Uses the workflow's actual parameter-loading code with shell-only tasks. Run in
a finite driver Job; no processing containers or scientific work are launched.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", default="nextflow")
    parser.add_argument("--label", default="current")
    args = parser.parse_args()
    root = Path(os.environ["TEST_ROOT"])
    source = Path(os.environ["SOURCE_DIR"])
    folder = root / "probes" / f"parameter-cache-{args.label}"
    folder.mkdir(parents=True, exist_ok=True)
    workflow = (source / "pipeline/main_multi_backend.nf").read_text()
    header = workflow.split("process job_dispatch {", 1)[0]
    script = folder / "main.nf"
    script.write_text(header + '''
process SORTER_PARAMETERS {
    output:
    path 'sorter.txt'
    script:
    """
    cat > sorter.txt <<'SETTINGS'
${spikesorting_args}
${versions['SPIKESORT_KS4']}
${gitCloneFunction}
SETTINGS
    """
}
process PREPROCESSING_PARAMETERS {
    output:
    path 'preprocessing.txt'
    script:
    """
    cat > preprocessing.txt <<'SETTINGS'
${preprocessing_args}
SETTINGS
    """
}
workflow {
    SORTER_PARAMETERS()
    PREPROCESSING_PARAMETERS()
}
''')
    for name in ("capsule_versions.env", "kilosort4_defaults.json"):
        shutil.copy2(source / "pipeline" / name, folder / name)
    config = folder / "local.config"
    config.write_text("process.executor = 'local'\nprocess.cpus = 1\nparams.executor = 'k8s'\n")
    params = json.loads((source / "scripts/params_no_motion.json").read_text())
    params["preprocessing"] = {"motion": "skip"}
    params_file = folder / "params.json"
    outcomes = {}

    def run(label, expected, resume=True):
        params_file.write_text(json.dumps(params, indent=2))
        trace = folder / f"{label}.tsv"
        command = [args.executable, "-log", str(folder / f"{label}.nextflow.log"),
                   "-C", str(config), "run", str(script), "--ecephys_path", str(root / "data"),
                   "--params_file", str(params_file), "--torch_device", "cuda",
                   "-with-trace", str(trace)]
        if resume:
            command.append("-resume")
        with (folder / f"{label}.log").open("w") as log:
            subprocess.run(command, cwd=folder, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=300)
        with trace.open() as file:
            rows = list(csv.DictReader(file, delimiter="\t"))
        statuses = {row["name"]: row["status"] for row in rows}
        outcomes[label] = statuses
        print(label, statuses, flush=True)
        assert statuses == expected, (label, statuses, expected)
        for row in rows:
            prefix, suffix = row["hash"].split("/")
            work = next((folder / "work" / prefix).glob(suffix + "*"))
            if row["name"] == "SORTER_PARAMETERS":
                lines = (work / "sorter.txt").read_text().splitlines()
                actual = json.loads(lines[0].removeprefix("--params '").removesuffix("'"))
                desired = json.loads(json.dumps(params["spikesorting"]["kilosort4"]))
                desired["sorter"]["torch_device"] = "cuda"
                assert actual == desired, (label, "Sorter settings were stale")
                pin = next(line.split("=", 1)[1] for line in
                           (folder / "capsule_versions.env").read_text().splitlines()
                           if line.startswith("SPIKESORT_KS4="))
                assert lines[1] == pin, (label, "Capsule pin was stale")
            else:
                actual = json.loads((work / "preprocessing.txt").read_text().strip()
                                    .removeprefix("--params '").removesuffix("'"))
                assert actual == params["preprocessing"], (label, "Preprocessing settings were stale")

    complete = {"SORTER_PARAMETERS": "COMPLETED", "PREPROCESSING_PARAMETERS": "COMPLETED"}
    cached = {name: "CACHED" for name in complete}
    run("initial", complete, resume=False)
    run("unchanged", cached)
    params["spikesorting"]["kilosort4"]["sorter"]["templates_from_data"] = False
    run("sorter-change", dict(cached, SORTER_PARAMETERS="COMPLETED"))
    run("sorter-unchanged", cached)
    params["preprocessing"]["motion"] = "compute"
    run("preprocessing-change", dict(cached, PREPROCESSING_PARAMETERS="COMPLETED"))
    pins = folder / "capsule_versions.env"
    lines = pins.read_text().splitlines()
    pins.write_text("\n".join("SPIKESORT_KS4=" + "0" * 40 if line.startswith("SPIKESORT_KS4=")
                              else line for line in lines) + "\n")
    run("capsule-pin-change", dict(cached, SORTER_PARAMETERS="COMPLETED"))
    run("pin-unchanged", cached)
    result = {"passed": True, "source": str(source), "checks": outcomes}
    (folder / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
