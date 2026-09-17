#!/usr/bin/env python3
"""Validate NWB artifacts with a pinned, isolated reader/schema-validator stack."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

here = Path(__file__).resolve().parent
deps = Path(tempfile.mkdtemp(prefix="nwb-validation-"))
subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--only-binary=:all:",
                "--no-cache-dir", "--target", str(deps), "-r", str(here / "nwb_validation_requirements.txt")],
               check=True)
env = os.environ.copy()
env["PYTHONPATH"] = str(deps) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
os.execvpe(sys.executable, [sys.executable, "-u", str(here / "validate.py"), "full", *sys.argv[1:]], env)
