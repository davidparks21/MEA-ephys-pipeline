#!/usr/bin/env python3
"""Archive test artifacts on the PVC, retaining work/cache for offline diagnosis."""
import hashlib
import json
import os
from pathlib import Path
import tarfile

root = Path(os.environ["TEST_ROOT"])
archive = root / "artifacts.tar.gz"
inventory = []


def include(member):
    # Repository checkout history and interpreter caches add no run evidence.
    if ".git" in Path(member.name).parts or "__pycache__" in Path(member.name).parts:
        return None
    if member.isfile():
        # Reuse tar's metadata instead of scanning every file twice on CephFS.
        inventory.append({"path": member.name, "bytes": member.size})
    return member


with tarfile.open(archive, "w:gz", compresslevel=1, dereference=False) as output:
    for folder in ("data", "probes", "paired", "runs", "source", "validation"):
        if (root / folder).exists():
            output.add(root / folder, arcname=folder, filter=include)
    (root / "inventory.json").write_text(json.dumps(inventory, indent=2))
    output.add(root / "inventory.json", arcname="inventory.json")
digest = hashlib.sha256()
with archive.open("rb") as handle:
    for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
        digest.update(chunk)
manifest = {"test_id": os.environ["TEST_ID"], "sha256": digest.hexdigest(),
            "bytes": archive.stat().st_size, "files": len(inventory)}
(root / "archive.json").write_text(json.dumps(manifest, indent=2))
print(json.dumps(manifest, indent=2))
