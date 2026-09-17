#!/usr/bin/env python3
"""Control-machine upload/download helpers restricted to dfparks/mea-ephys-tests.

The upload runs as a finite NRP Job with a task-owned Secret. The AWS CLI is
needed locally only for download. Credentials never enter manifests or argv.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

REPO = Path(__file__).resolve().parents[2]
ENDPOINT = "https://s3.braingeneers.gi.ucsc.edu"

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--test-id", required=True)
parser.add_argument("--namespace", default="braingeneers")
parser.add_argument("action", choices=("upload", "download"))
args = parser.parse_args()
assert re.fullmatch(r"[a-z][a-z0-9-]{0,34}", args.test_id)
prefix = f"s3://braingeneersdev/dfparks/mea-ephys-tests/{args.test_id}/"
if args.action == "upload":
    secret_name = f"{args.test_id}-s3"
    credentials = {k: os.environ[k] for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")}
    if os.environ.get("AWS_SESSION_TOKEN"):
        credentials["AWS_SESSION_TOKEN"] = os.environ["AWS_SESSION_TOKEN"]
    credentials["AWS_DEFAULT_REGION"] = "us-east-1"
    secret = {"apiVersion": "v1", "kind": "Secret",
              "metadata": {"name": secret_name, "labels": {"mea-test": args.test_id}},
              "type": "Opaque", "stringData": credentials}
    # create rather than apply: no kubectl last-applied annotation containing credentials.
    subprocess.run(["kubectl", "--context", "nautilus", "-n", args.namespace, "create", "-f", "-"],
                   input=json.dumps(secret), text=True, check=True, stdout=subprocess.DEVNULL)
    cmd = (f"aws --endpoint-url {ENDPOINT} s3 cp {{root}}/artifacts.tar.gz {prefix}artifacts.tar.gz --only-show-errors\n"
           f"aws --endpoint-url {ENDPOINT} s3 cp {{root}}/archive.json {prefix}archive.json --only-show-errors")
    subprocess.run(["python3", str(REPO / "scripts/nrp/submit.py"), "--test-id", args.test_id,
                    "--namespace", args.namespace, "job", "upload", "--image", "transfer",
                    "--secret", secret_name, "--deadline", "1800", "--command", cmd], check=True)
else:
    dest = REPO / "results/nrp" / args.test_id
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("archive.json", "artifacts.tar.gz"):
        subprocess.run(["aws", "--endpoint-url", ENDPOINT, "s3", "cp", prefix + name,
                        str(dest / name), "--only-show-errors"], check=True)
    manifest = json.loads((dest / "archive.json").read_text())
    digest = hashlib.sha256()
    with (dest / "artifacts.tar.gz").open("rb") as file:
        for chunk in iter(lambda: file.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    assert digest.hexdigest() == manifest["sha256"], "Download checksum mismatch; retain the PVC"
    assert (dest / "artifacts.tar.gz").stat().st_size == manifest["bytes"]
    receipt = {"verified": True, "s3_prefix": prefix, **manifest}
    (dest / "verified.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt, indent=2))
