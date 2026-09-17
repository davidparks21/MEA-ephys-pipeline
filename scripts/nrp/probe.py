#!/usr/bin/env python3
"""Record actual scientific image versions and test the requested torch device."""
import argparse
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import platform
import subprocess
import time

parser = argparse.ArgumentParser()
parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()

import numpy as np
import spikeinterface as si
import spikeinterface.preprocessing as spre
import torch

torch.set_num_threads(int(os.environ.get("CO_CPUS", "1")))
versions = {}
for package in ("spikeinterface", "kilosort", "torch", "numpy", "scipy", "numba", "pynwb", "neuroconv", "zarr", "openpyxl", "boto3"):
    try:
        versions[package] = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        versions[package] = None
result = {"device_requested": args.device, "versions": versions, "python": platform.python_version(),
          "host": platform.node(), "cuda_available": torch.cuda.is_available(), "torch_cuda": torch.version.cuda,
          "threads": torch.get_num_threads(), "si_ground_truth_signature": str(inspect.signature(si.generate_ground_truth_recording))}
if args.device == "cuda":
    assert torch.cuda.is_available(), "GPU Job has no usable CUDA device; refusing CPU fallback"
    result["gpu"] = torch.cuda.get_device_name(0)
    result["nvidia_smi"] = subprocess.check_output(["nvidia-smi"], text=True)
start = time.monotonic()
x = torch.randn((2048, 2048), device=args.device)
y = x @ x.T
assert y.device.type == args.device and torch.isfinite(y).all().item()
if args.device == "cuda":
    torch.cuda.synchronize()
result["tensor_seconds"] = time.monotonic() - start
result["device_used"] = str(y.device)
# Reproduce the pinned capsule's unconditional unsigned conversion on signed data.
recording = si.NumpyRecording(np.arange(128, dtype="int16").reshape(32, 4), 30000)
try:
    converted = spre.unsigned_to_signed(recording)
    result["unsigned_conversion_on_signed"] = {"status": "accepted", "dtype": str(converted.get_dtype())}
except Exception as exc:
    result["unsigned_conversion_on_signed"] = {"status": "error", "type": type(exc).__name__, "message": str(exc)}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
