#!/usr/bin/env python3
"""Run the pinned capsules on a common fixture before the full Nextflow runs."""
import argparse
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import threading
import time

ROOT = Path(os.environ["TEST_ROOT"])
SOURCE = Path(os.environ["SOURCE_DIR"])
PINS = dict(line.split("=", 1) for line in (SOURCE / "pipeline/capsule_versions.env").read_text().splitlines() if "=" in line)


def layout(folder):
    for name in ("data", "results", "scratch"):
        (folder / name).mkdir(parents=True, exist_ok=True)


def link_results(source, dest):
    for path in source.iterdir():
        (dest / path.name).symlink_to(path)


def checkout(repo, pin, folder):
    subprocess.run(["git", "clone", "--quiet", "--no-checkout", repo, str(folder / "upstream")], check=True)
    subprocess.run(["git", "-C", str(folder / "upstream"), "checkout", "--quiet", pin], check=True)
    shutil.copytree(folder / "upstream/code", folder / "code")


def execute(folder, arguments):
    with (folder / "command.log").open("w") as log:
        process = subprocess.Popen(["bash", "run", *arguments], cwd=folder / "code",
                                   stdout=log, stderr=subprocess.STDOUT)
        code = process.wait()
    print((folder / "command.log").read_text()[-10000:], flush=True)
    if code:
        raise RuntimeError(f"Capsule failed with exit {code}: {folder}/command.log")


def preprocess():
    import numpy as np
    import spikeinterface as si
    for representation in ("signed", "unsigned"):
        folder = ROOT / "paired" / "preprocess" / representation
        dispatch = folder / "dispatch"
        prep = folder / "preprocessing"
        layout(dispatch)
        layout(prep)
        session = ROOT / "data/fixture" / representation
        (dispatch / "data/ecephys_session").symlink_to(session)
        checkout("https://github.com/AllenNeuralDynamics/aind-ephys-job-dispatch.git", PINS["JOB_DISPATCH"], dispatch)
        execute(dispatch, ["--input", "nwb"])
        (prep / "data/ecephys_session").symlink_to(session)
        link_results(dispatch / "results", prep / "data")
        shutil.copytree(SOURCE / "capsules/preprocessing/code", prep / "code")
        execute(prep, ["--motion", "skip"])
    recordings = []
    for rep in ("signed", "unsigned"):
        folder = ROOT / "paired/preprocess" / rep / "preprocessing/results"
        files = list(folder.glob("binary_*.json"))
        assert len(files) == 1, f"Expected one preprocessed recording: {files}"
        recordings.append(si.load(files[0], base_folder=folder))
    signed, unsigned = recordings
    assert signed.get_num_channels() == unsigned.get_num_channels()
    assert signed.get_num_samples() == unsigned.get_num_samples()
    max_difference = 0.0
    for start in range(0, signed.get_num_samples(), 30000):
        end = min(start + 30000, signed.get_num_samples())
        a = signed.get_traces(start_frame=start, end_frame=end, return_in_uV=True)
        b = unsigned.get_traces(start_frame=start, end_frame=end, return_in_uV=True)
        max_difference = max(max_difference, float(np.max(np.abs(a - b))))
    result = {"signed_unsigned_preprocessed_max_difference_uV": max_difference,
              "channels": signed.get_num_channels(), "samples": signed.get_num_samples()}
    (ROOT / "paired/preprocessing-comparison.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    assert max_difference < 0.05, "Signed/unsigned filtering changed the physical signal"


def sort(device):
    import torch
    assert device == "cpu" or torch.cuda.is_available(), "CUDA unavailable: refusing CPU fallback"
    folder = ROOT / "paired" / device
    layout(folder)
    link_results(ROOT / "paired/preprocess/signed/preprocessing/results", folder / "data")
    checkout("https://github.com/AllenNeuralDynamics/aind-ephys-spikesort-kilosort4.git", PINS["SPIKESORT_KS4"], folder)
    settings = json.loads((SOURCE / "scripts/params_no_motion.json").read_text())["spikesorting"]["kilosort4"]
    settings["sorter"]["torch_device"] = device
    (folder / "parameters.json").write_text(json.dumps(settings, indent=2))
    stop = threading.Event()

    def monitor_gpu():
        with (folder / "gpu.csv").open("w") as log:
            log.write("timestamp, index, name, utilization_gpu_pct, memory_used_MiB, memory_total_MiB\n")
            while not stop.is_set():
                subprocess.run(["nvidia-smi", "--query-gpu=timestamp,index,name,utilization.gpu,memory.used,memory.total",
                                "--format=csv,noheader,nounits"], stdout=log, check=False)
                log.flush()
                stop.wait(1)

    monitor = threading.Thread(target=monitor_gpu) if device == "cuda" else None
    if monitor:
        monitor.start()
    start = time.monotonic()
    try:
        execute(folder, ["--params", json.dumps(settings)])
    finally:
        stop.set()
        if monitor:
            monitor.join()
        result = {"device_requested": device, "elapsed_seconds": time.monotonic() - start,
                  "peak_rss_KiB": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                  "host": os.uname().nodename, "cpus": int(os.environ["CO_CPUS"]),
                  "torch": torch.__version__, "cuda": torch.version.cuda,
                  "gpu": torch.cuda.get_device_name(0) if device == "cuda" else None}
        for path in (Path('/sys/fs/cgroup/memory.peak'), Path('/sys/fs/cgroup/memory/memory.max_usage_in_bytes')):
            if path.exists():
                result["cgroup_memory_peak_bytes"] = int(path.read_text())
                break
        (folder / "runtime.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("preprocess", "cpu", "cuda"))
    args = parser.parse_args()
    if args.phase == "preprocess":
        preprocess()
    else:
        sort(args.phase)
