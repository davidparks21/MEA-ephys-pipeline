#!/usr/bin/env python3
"""Stage the pinned optional universal templates used by Kilosort 4.0.38."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

URL = "https://osf.io/download/6807fb5958b763aae139aa60/"
SHA256 = "cae1c96f8f4150be0a39627515750b70c4bc3548177cf487ae3c013f1ca6abd8"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    target = args.directory / "wTEMP.npz"
    if target.exists():
        payload = target.read_bytes()
    else:
        with urllib.request.urlopen(URL, timeout=60) as response:
            payload = response.read(1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != SHA256:
        raise RuntimeError(f"Kilosort templates checksum mismatch: {digest}")
    if not target.exists():
        with tempfile.NamedTemporaryFile(dir=args.directory, delete=False) as staged:
            staged.write(payload)
            staged_path = staged.name
        os.chmod(staged_path, 0o644)
        os.replace(staged_path, target)
    provenance = {"url": URL, "sha256": digest, "bytes": len(payload), "path": str(target)}
    (args.directory / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(provenance, indent=2))


if __name__ == "__main__":
    main()
