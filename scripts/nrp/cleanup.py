#!/usr/bin/env python3
"""Remove only this test's cluster resources after verified artifact collection."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile

repo = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--test-id", required=True)
args = parser.parse_args()
assert re.fullmatch(r"[a-z][a-z0-9-]{0,34}", args.test_id)
logs = repo / "logs/nrp" / args.test_id
state = json.loads((logs / "state.json").read_text())
results = repo / "results/nrp" / args.test_id
receipt = json.loads((results / "verified.json").read_text())
assert receipt["verified"] and receipt["test_id"] == args.test_id
archive = results / "artifacts.tar.gz"
assert archive.stat().st_size == receipt["bytes"]
digest = hashlib.sha256()
with archive.open("rb") as file:
    for chunk in iter(lambda: file.read(8 * 1024 * 1024), b""):
        digest.update(chunk)
assert digest.hexdigest() == receipt["sha256"]
with tarfile.open(archive, "r:gz") as bundle:
    names = set(bundle.getnames())
assert {"validation/paired.json", "validation/full.json", "validation/execution.json"}.issubset(names), \
    "Scientific validation or resume verification is missing"
assert (logs / "cluster-jobs.json").exists() and (logs / "cluster-pods.json").exists()
selector = f"mea-test={args.test_id}"
cmd = ["kubectl", "--context", "nautilus", "-n", state["namespace"]]
jobs = json.loads(subprocess.check_output([*cmd, "get", "jobs", "-l", selector, "-o", "json"], text=True))
for job in jobs["items"]:
    conditions = job.get("status", {}).get("conditions", [])
    assert any(c["type"] in ("Complete", "Failed") and c["status"] == "True" for c in conditions), \
        f'Job still active: {job["metadata"]["name"]}'
for kind in ("jobs", "configmaps", "secrets", "pvc"):
    subprocess.run([*cmd, "delete", kind, "-l", selector, "--wait=true", "--timeout=60s"], check=True)
(logs / "cleanup.json").write_text(json.dumps({"test_id": args.test_id, "cleaned": True,
                                                "archive_sha256": receipt["sha256"]}, indent=2))
print("Removed test-owned cluster resources; verified local and S3 archives retained.")
