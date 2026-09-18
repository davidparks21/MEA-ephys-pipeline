#!/usr/bin/env python3
"""Stage a checksum-pinned Maxwell HDF5 decoder on the shared NRP workspace."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

URL = "https://share.mxwbio.com/d/7f2d1e98a1724a1b8b35/files/?p=%2FLinux%2Flibcompression.so&dl=1"
SHA256 = "4bc74ea98c08b70406aa976950fcf5f55d572430f51d3882eeb947c557dd14f4"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    target = args.directory / "libcompression.so"
    if target.exists():
        payload = target.read_bytes()
    else:
        with urllib.request.urlopen(URL, timeout=60) as response:
            payload = response.read(16 * 1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != SHA256:
        raise RuntimeError(f"Maxwell decoder checksum mismatch: {digest}")
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
