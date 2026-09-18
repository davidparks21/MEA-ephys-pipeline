#!/usr/bin/env python3
"""Entrypoint for the Nextflow driver Job; separate CPU/GPU caches and work dirs."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

parser = argparse.ArgumentParser()
parser.add_argument("--device", choices=("cpu", "gpu"), required=True)
parser.add_argument("--resume", action="store_true")
parser.add_argument("--preview", action="store_true")
parser.add_argument("--input", type=Path, help="Staged recording directory on the workspace PVC")
parser.add_argument("--run-name", help="Independent launch/work/results directory; defaults to the device")
parser.add_argument("--params-file", type=Path, help="Stage settings JSON; defaults to params_no_motion.json")
parser.add_argument("--config", type=Path, help="Additional NRP configuration, e.g. resource overrides")
parser.add_argument("--source-snapshot", help="Reuse a recorded 12-character source ID, e.g. for a cache-only resume check")
args = parser.parse_args()
root = Path(os.environ["TEST_ROOT"])
source = Path(os.environ["SOURCE_DIR"])
if args.source_snapshot:
    if not re.fullmatch(r"[a-f0-9]{12}", args.source_snapshot):
        parser.error("--source-snapshot must be a recorded 12-character source ID")
    source = root / "source" / args.source_snapshot
run_name = args.run_name or args.device
if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", run_name):
    parser.error("--run-name must contain only letters, digits, underscores and hyphens")
input_dir = args.input or root / "data/fixture/signed"
params_file = args.params_file or source / "scripts/params_no_motion.json"
if not input_dir.is_dir() or not params_file.is_file():
    parser.error("Input directory and stage settings JSON must exist on the workspace")
configs = [source / "pipeline/nextflow_nrp.config"]
if args.config:
    if not args.config.is_file():
        parser.error("Additional config does not exist")
    configs.append(args.config)
run = root / "runs" / run_name
for folder in ("launch", "work", "results", "evidence", "cache"):
    (run / folder).mkdir(parents=True, exist_ok=True)
os.environ["NXF_HOME"] = str(run / "cache")
stamp = str(time.time_ns())
command = [
    "nextflow", "-log", str(run / "evidence" / f"nextflow-{stamp}.log"),
    "-C", ",".join(map(str, configs)),
    "run", str(source / "pipeline/main_multi_backend.nf"), "-profile", args.device,
    "-work-dir", str(run / "work"),
    "--ecephys_path", str(input_dir),
    "--results_path", str(run / "results"),
    "--params_file", str(params_file),
    "--preprocessing_args=--motion skip",
    "-with-report", str(run / "evidence" / f"report-{stamp}.html"),
    "-with-timeline", str(run / "evidence" / f"timeline-{stamp}.html"),
    "-with-trace", str(run / "evidence" / f"trace-{stamp}.tsv"),
]
if args.resume:
    command.append("-resume")
if args.preview:
    command.append("-preview")
shutil.copy2(source / "source-manifest.json", run / "evidence" / f"source-{stamp}.json")
shutil.copy2(params_file, run / "evidence" / f"params-{stamp}.json")
for i, config in enumerate(configs):
    shutil.copy2(config, run / "evidence" / f"config-{stamp}-{i}.config")
(run / "evidence" / f"command-{stamp}.json").write_text(json.dumps(command, indent=2))
print(json.dumps(command), flush=True)
process = subprocess.Popen(command, cwd=run / "launch", start_new_session=True)


def forward_signal(signum, _frame):
    # Kubernetes signals the driver wrapper. Let Nextflow close its cache cleanly.
    if process.poll() is None:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass  # The child exited between poll() and killpg().


signal.signal(signal.SIGTERM, forward_signal)
signal.signal(signal.SIGINT, forward_signal)
code = process.wait()
(run / "evidence" / f"exit-{stamp}.json").write_text(json.dumps({"exit_code": code}))
raise SystemExit(code)
