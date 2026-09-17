#!/usr/bin/env python3
"""Regression check: strict report curation may legitimately select zero units.

Run in the base image on NRP. Mock only the already-computed analyzer and plotting;
exercise report serialization and the actual downstream burst command.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import numpy as np
import pandas as pd

source = Path(os.environ["SOURCE_DIR"])
output = Path(os.environ["TEST_ROOT"]) / "validation/empty-reports"
output.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location("report", source / "capsules/report_generation/run_capsule.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class Extension:
    def __init__(self, data):
        self.data = data

    def get_data(self):
        return self.data


class Analyzer:
    unit_ids = np.array([7])
    sorting = object()

    class recording:
        @staticmethod
        def get_sampling_frequency():
            return 30000

    def get_num_units(self):
        return 1

    def get_extension(self, name):
        values = {
            "quality_metrics": pd.DataFrame({"presence_ratio": [1.0], "rp_contamination": [0.0],
                                               "firing_rate": [0.01], "amplitude_median": [-100.0]}, index=[7]),
            "template_metrics": pd.DataFrame({"peak_to_valley": [0.2]}, index=[7]),
            "unit_locations": np.array([[0., 20.]]),
        }
        return Extension(values[name])


with patch.object(report.si, "load_sorting_analyzer", return_value=Analyzer()), \
     patch.object(report, "plot_probe_locations"), \
     patch.object(pd.DataFrame, "to_excel"), \
     patch.object(sys, "argv", ["report", "--analyzer-dir", "unused", "--output-dir", str(output / "report")]):
    report.main()
summary = json.loads((output / "report/report_summary.json").read_text())
assert summary["status"] == "no_curated_units" and summary["n_units_curated"] == 0
assert np.load(output / "report/spike_times.npy", allow_pickle=True).item() == {}
subprocess.run([sys.executable, str(source / "capsules/burst_detection/run_capsule.py"),
                "--spike-times", str(output / "report/spike_times.npy"),
                "--output-dir", str(output / "bursts")], check=True)
result = json.loads((output / "bursts/network_results.json").read_text())
assert result["status"] == "no_spikes" and result["network_bursts"]["events"] == []
print("Empty report -> serialized empty spike dictionary -> empty burst result: PASS")
