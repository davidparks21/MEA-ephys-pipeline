#!/usr/bin/env python3
"""Replay a completed visualization task with current code and isolated outputs."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--task-dir", type=Path, required=True)
parser.add_argument("--label", required=True)
parser.add_argument("--expect-fallback", action="store_true")
args = parser.parse_args()
if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.label):
    parser.error("Use a lowercase label with letters, digits and hyphens")
root = Path(os.environ["TEST_ROOT"])
source = Path(os.environ["SOURCE_DIR"])
task = args.task_dir.resolve()
if not task.is_relative_to(root / "runs") or not (task / ".exitcode").is_file():
    parser.error("--task-dir must be a finished workflow task in this test workspace")

# Serialized descriptors depend on the work-directory depth. Keep that depth
# while retaining the original task and all input artifacts unchanged.
probe = task.parent.parent / "probes" / args.label
capsule = probe / "capsule"
capsule.mkdir(parents=True, exist_ok=False)
shutil.copytree(task / "capsule/data", capsule / "data", symlinks=True)
shutil.copytree(source / "capsules/visualization/code", capsule / "code")
for name in ("results", "scratch"):
    (capsule / name).mkdir()
log = probe / "visualization.log"
print(f"Replaying {task} in {probe}", flush=True)
with log.open("w") as output:
    process = subprocess.run(["bash", "run"], cwd=capsule / "code",
                             stdout=output, stderr=subprocess.STDOUT)
text = log.read_text()
print(text[-12000:], flush=True)
assert process.returncode == 0, f"Visualization failed; see {log}"
if args.expect_fallback:
    assert "Visualizing drift maps using detected peaks" in text
    assert re.search(r"Detected \d+ peaks", text), "Peak detector did not complete"
    assert "Could not generate drift map" not in text, "Fallback error was suppressed"
plots = sorted((capsule / "results").glob("visualization_*/*.png"))
assert any(p.name.startswith("traces_full_seg") for p in plots), "Missing raw trace plot"
assert any(p.name.startswith("traces_proc_seg") for p in plots), "Missing processed trace plot"
peaks = re.search(r"Detected (\d+) peaks", text)
if args.expect_fallback and int(peaks.group(1)):
    assert any(p.name == "drift_map.png" for p in plots), "Missing fallback drift map"
assert all(p.stat().st_size > 0 for p in plots)
result = {"task": str(task), "probe": str(probe), "exit_code": 0,
          "fallback_tested": args.expect_fallback,
          "detected_peaks": int(peaks.group(1)) if peaks else None,
          "plots": [str(p) for p in plots]}
(root / "validation").mkdir(exist_ok=True)
(root / "validation" / f"visualization-{args.label}.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
