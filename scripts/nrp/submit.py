#!/usr/bin/env python3
"""Submit finite NRP Jobs from a snapshot of this checkout; no local science runs.

Only kubectl and Python's standard library are needed on the control machine.
Source snapshots and manifests are retained under logs/nrp/TEST_ID.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

REPO = Path(__file__).resolve().parents[2]
IMAGES = json.loads((Path(__file__).with_name("images.json")).read_text())


def kubectl(namespace, *args, payload=None):
    proc = subprocess.run(
        ["kubectl", "--context", "nautilus", "-n", namespace, *args],
        input=json.dumps(payload) if payload is not None else None,
        text=True, stdout=subprocess.PIPE, check=True,
    )
    return proc.stdout


def snapshot():
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--",
         "pipeline", "capsules", "scripts"], cwd=REPO).decode().split("\0")
    files = sorted(REPO / path for path in set(paths) if path and (REPO / path).is_file())
    hashes = {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in files}
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    provenance = {"git_revision": revision, "files": hashes}
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        for p in files:
            entry = zipfile.ZipInfo(str(p.relative_to(REPO)))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, p.read_bytes())
        entry = zipfile.ZipInfo("source-manifest.json")
        entry.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(entry, json.dumps(provenance, indent=2))
    content = data.getvalue()
    if len(content) > 700_000:
        raise ValueError("Source bundle exceeds ConfigMap budget; do not include datasets")
    return content, hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()[:12]


def apply_record(namespace, obj, logdir):
    name = obj["metadata"]["name"]
    (logdir / f'{name}.{obj["kind"].lower()}.json').write_text(json.dumps(obj, indent=2))
    print(kubectl(namespace, "apply", "-f", "-", payload=obj), end="")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-id", required=True, help="Unique lowercase Kubernetes name, <=35 characters")
    parser.add_argument("--namespace", default="braingeneers")
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("--storage", default="20Gi", help="Dedicated workspace size; increase for real recordings")
    sub.add_parser("status")
    sub.add_parser("collect-logs")
    job = sub.add_parser("job")
    job.add_argument("name")
    job.add_argument("--image", choices=IMAGES, default="base")
    job.add_argument("--cpus", default="1")
    job.add_argument("--memory", default="2Gi")
    job.add_argument("--ephemeral", default="4Gi")
    job.add_argument("--gpu", action="store_true")
    job.add_argument("--deadline", type=int, default=1800)
    job.add_argument("--driver", action="store_true", help="Mount service-account token for Nextflow")
    job.add_argument("--exclude-node", action="append", default=[], help="Temporarily exclude a diagnosed unhealthy node")
    job.add_argument("--secret", help="Optional task-owned S3 Secret, mounted as environment variables")
    job.add_argument("--command", required=True, help="Bash command; {source} and {root} are expanded")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,34}", args.test_id):
        parser.error("Use a lowercase test ID of at most 35 characters")
    logdir = REPO / "logs" / "nrp" / args.test_id
    logdir.mkdir(parents=True, exist_ok=True)
    labels = {"mea-test": args.test_id, "app.kubernetes.io/managed-by": "mea-ephys-tests"}
    root = f"/workspace/{args.test_id}"
    pvc = f"{args.test_id}-workspace"
    state = {"test_id": args.test_id, "context": "nautilus", "namespace": args.namespace, "pvc": pvc, "root": root}
    (logdir / "state.json").write_text(json.dumps(state, indent=2))

    if args.action == "init":
        obj = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
               "metadata": {"name": pvc, "labels": labels},
               "spec": {"accessModes": ["ReadWriteMany"], "storageClassName": "rook-cephfs",
                        "resources": {"requests": {"storage": args.storage}}}}
        apply_record(args.namespace, obj, logdir)
        return
    if args.action in ("status", "collect-logs"):
        selector = f"mea-test={args.test_id}"
        print(kubectl(args.namespace, "get", "jobs,pods,pvc", "-l", selector, "-o", "wide"))
        if args.action == "collect-logs":
            for kind in ("jobs", "pods", "configmaps", "pvc"):
                payload = kubectl(args.namespace, "get", kind, "-l", selector, "-o", "json")
                (logdir / f"cluster-{kind}.json").write_text(payload)
            pods = json.loads((logdir / "cluster-pods.json").read_text())["items"]
            for pod in pods:
                name = pod["metadata"]["name"]
                events = kubectl(args.namespace, "get", "events", "--field-selector", f"involvedObject.name={name}", "-o", "json")
                (logdir / f"{name}.events.json").write_text(events)
                for c in pod["spec"].get("initContainers", []) + pod["spec"]["containers"]:
                    try:
                        data = kubectl(args.namespace, "logs", name, "-c", c["name"], "--timestamps=true")
                        (logdir / f'{name}.{c["name"]}.log').write_text(data)
                    except subprocess.CalledProcessError:
                        pass  # Pending container; other evidence should still be collected.
        return

    if not re.fullmatch(r"[a-z][a-z0-9-]{0,24}", args.name):
        parser.error("Job suffix must be a lowercase name of at most 25 characters")
    name = f"{args.test_id}-{args.name}"
    # Refuse accidental duplicate execution/updates of existing Jobs.
    if kubectl(args.namespace, "get", "job", name, "--ignore-not-found", "-o", "name").strip():
        parser.error(f"Job {name} already exists; choose a new suffix")
    content, digest = snapshot()
    source = f"{root}/source/{digest}"
    cm = f"{args.test_id}-src-{digest}"
    (logdir / f"source-{digest}.zip").write_bytes(content)
    apply_record(args.namespace, {
        "apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
        "metadata": {"name": cm, "labels": labels},
        "binaryData": {"source.zip": base64.b64encode(content).decode()},
    }, logdir)
    init_code = f"""
import os, pathlib, tempfile, zipfile
root = pathlib.Path({root!r})
for path in [root, root/'source', root/'data', root/'probes', root/'runs', root/'cache', root/'cache'/{args.name!r}]:
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, 1000, 100)
target = pathlib.Path({source!r})
if not target.exists():
    staging = pathlib.Path(tempfile.mkdtemp(dir=target.parent))
    with zipfile.ZipFile('/bundle/source.zip') as archive:
        archive.extractall(staging)
    for parent, dirs, files in os.walk(staging):
        os.chown(parent, 1000, 100)
        for file in files:
            os.chown(os.path.join(parent, file), 1000, 100)
    try:
        staging.rename(target)
    except OSError:
        if not target.exists():
            raise
"""
    resources = {"cpu": str(args.cpus), "memory": args.memory, "ephemeral-storage": args.ephemeral}
    if args.gpu:
        resources["nvidia.com/gpu"] = "1"
    cmd = args.command.replace("{source}", source).replace("{root}", root)
    environment = {
        "HOME": f"{root}/cache/{args.name}", "SOURCE_DIR": source, "TEST_ROOT": root,
        "TEST_ID": args.test_id, "TEST_PVC": pvc, "TEST_NAMESPACE": args.namespace,
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "MPLBACKEND": "Agg", "OMP_NUM_THREADS": str(args.cpus),
        "OPENBLAS_NUM_THREADS": str(args.cpus), "MKL_NUM_THREADS": str(args.cpus),
        "NUMBA_NUM_THREADS": str(args.cpus), "CO_CPUS": str(args.cpus), "N_JOBS_EXT": str(args.cpus),
        "NXF_ANSI_LOG": "false", "NXF_HOME": f"{root}/cache/nextflow",
        "NXF_DIST": "/home/nextflow/.nextflow/framework",
        "NXF_PLUGINS_DIR": "/home/nextflow/.nextflow/plugins",
        "NXF_SYNTAX_PARSER": "v1",  # This fork uses the supported Groovy DSL2 syntax.
        "TEST_EXCLUDE_NODES": ",".join(args.exclude_node),
    }
    main_container = {
        "name": "main", "image": IMAGES[args.image], "imagePullPolicy": "IfNotPresent",
        "command": ["/bin/bash", "-c", "set -euo pipefail\n" + cmd],
        "workingDir": root, "env": [{"name": k, "value": v} for k, v in environment.items()],
        "resources": {"requests": resources, "limits": resources},
        "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
        "securityContext": {"runAsUser": 1000, "runAsGroup": 100, "allowPrivilegeEscalation": False},
    }
    if args.secret:
        if not args.secret.startswith(args.test_id + "-"):
            parser.error("Only a Secret owned by this test ID can be attached")
        main_container["envFrom"] = [{"secretRef": {"name": args.secret}}]
    init_resources = {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "256Mi"}
    obj = {
        "apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "labels": labels},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": args.deadline,
                 "ttlSecondsAfterFinished": 86400,
                 "template": {"metadata": {"labels": labels}, "spec": {
                     "serviceAccountName": "nextflow-sa", "automountServiceAccountToken": args.driver,
                     "restartPolicy": "Never", "securityContext": {"fsGroup": 100},
                     "volumes": [{"name": "workspace", "persistentVolumeClaim": {"claimName": pvc}},
                                 {"name": "source", "configMap": {"name": cm}}],
                     "initContainers": [{"name": "stage-source", "image": IMAGES["driver"],
                                         "command": ["python3", "-c", init_code],
                                         "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                                         "resources": {"requests": init_resources, "limits": init_resources},
                                         "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"},
                                                          {"name": "source", "mountPath": "/bundle", "readOnly": True}]}],
                     "containers": [main_container],
                 }}}}
    if args.gpu:
        obj["spec"]["template"]["spec"]["tolerations"] = [
            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "PreferNoSchedule"}]
    if args.driver:
        obj["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] = 120
    if args.exclude_node:
        obj["spec"]["template"]["spec"]["affinity"] = {"nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [
                {"key": "kubernetes.io/hostname", "operator": "NotIn", "values": args.exclude_node}]}]}}}
    apply_record(args.namespace, obj, logdir)
    print(f"Source: {digest}; command: {cmd}")


if __name__ == "__main__":
    main()
