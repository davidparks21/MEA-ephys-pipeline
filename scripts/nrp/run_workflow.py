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
parser.add_argument("--source-snapshot", help="Reuse a recorded 12-character source ID, e.g. for a cache-only resume check")
args = parser.parse_args()
root = Path(os.environ["TEST_ROOT"])
source = Path(os.environ["SOURCE_DIR"])
if args.source_snapshot:
    if not re.fullmatch(r"[a-f0-9]{12}", args.source_snapshot):
        parser.error("--source-snapshot must be a recorded 12-character source ID")
    source = root / "source" / args.source_snapshot
run = root / "runs" / args.device
for folder in ("launch", "work", "results", "evidence", "cache"):
    (run / folder).mkdir(parents=True, exist_ok=True)
os.environ["NXF_HOME"] = str(run / "cache")
stamp = str(time.time_ns())
command = [
    "nextflow", "-log", str(run / "evidence" / f"nextflow-{stamp}.log"),
    "-C", str(source / "pipeline/nextflow_nrp.config"),
    "run", str(source / "pipeline/main_multi_backend.nf"), "-profile", args.device,
    "-work-dir", str(run / "work"),
    "--ecephys_path", str(root / "data/fixture/signed"),
    "--results_path", str(run / "results"),
    "--params_file", str(source / "scripts/params_no_motion.json"),
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
