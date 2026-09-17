#!/usr/bin/env python3
"""Check the in-cluster driver identity without exposing its bearer token."""
import json
import os
from pathlib import Path
import ssl
import subprocess
import urllib.request

sa = Path("/var/run/secrets/kubernetes.io/serviceaccount")
context = ssl.create_default_context(cafile=str(sa / "ca.crt"))
namespace = (sa / "namespace").read_text().strip()
results = []
for group, resource, verb in (("batch", "jobs", "create"), ("batch", "jobs", "get"),
                               ("batch", "jobs", "delete"), ("", "pods", "list"), ("", "pods/log", "get")):
    request = urllib.request.Request(
        "https://kubernetes.default.svc/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
        data=json.dumps({"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                         "spec": {"resourceAttributes": {"namespace": namespace, "group": group,
                                                           "resource": resource, "verb": verb}}}).encode(),
        headers={"Authorization": "Bearer " + (sa / "token").read_text().strip(),
                 "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, context=context, timeout=30) as response:
        allowed = json.load(response)["status"]["allowed"]
    results.append({"resource": resource, "verb": verb, "allowed": allowed})
    assert allowed, f"nextflow-sa cannot {verb} {resource}"
version = subprocess.check_output(["nextflow", "-version"], text=True)
result = {"namespace": namespace, "permissions": results, "nextflow": version}
(Path(os.environ["TEST_ROOT"]) / "probes/driver.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
