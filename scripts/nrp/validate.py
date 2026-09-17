#!/usr/bin/env python3
"""Validate scientific outputs on NRP and record CPU/GPU ground-truth agreement."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path

import numpy as np
import spikeinterface.full as si

ROOT = Path(os.environ["TEST_ROOT"])
OUT = ROOT / "validation"
OUT.mkdir(exist_ok=True)
TRUTH = si.load(ROOT / "data/fixture/ground_truth")
FIXTURE = json.loads((ROOT / "data/fixture/fixture.json").read_text())


def check_sorting(sorting):
    spikes = sorting.to_spike_vector()
    assert sorting.get_num_units() > 0 and len(spikes) > 0, "Synthetic test produced no units/spikes"
    assert (spikes["sample_index"] >= 0).all()
    assert (spikes["sample_index"] < FIXTURE["sampling_frequency"] * FIXTURE["duration_s"]).all()
    comparison = si.compare_sorter_to_ground_truth(TRUTH, sorting, exhaustive_gt=True)
    performance = comparison.get_performance(method="pooled_with_average")
    result = {"units": sorting.get_num_units(), "spikes": len(spikes),
              "well_detected_gt_units": int(comparison.count_well_detected_units(well_detected_score=0.8)),
              "well_detected_agreement_threshold": 0.8,
              "performance": {key: float(value) for key, value in performance.items()}}
    assert result["well_detected_gt_units"] > 0, "No ground-truth units recovered"
    assert all(np.isfinite(v) for v in result["performance"].values())
    return result


def paired():
    sortings = {}
    result = {}
    for device in ("cpu", "cuda"):
        paths = list((ROOT / "paired" / device / "results").glob("spikesorted_*"))
        assert len(paths) == 1
        sorting = si.load(paths[0])
        sortings[device] = sorting
        result[device] = {**check_sorting(sorting),
                          "runtime": json.loads((ROOT / "paired" / device / "runtime.json").read_text())}
    comparison = si.compare_two_sorters(sortings["cpu"], sortings["cuda"])
    result["cpu_gpu_matched_units_0.5"] = int(sum(comparison.hungarian_match_12 >= 0))
    matched = [(left, right) for left, right in comparison.hungarian_match_12.items() if right >= 0]
    result["cpu_gpu_exact_spike_train_matches"] = sum(
        np.array_equal(sortings["cpu"].get_unit_spike_train(left),
                       sortings["cuda"].get_unit_spike_train(right))
        for left, right in matched)
    result["cpu_gpu_matched_agreement_scores"] = [
        float(comparison.agreement_scores.loc[left, right]) for left, right in matched]
    result["preprocessing"] = json.loads((ROOT / "paired/preprocessing-comparison.json").read_text())
    (OUT / "paired.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


def single_sort(device):
    paths = list((ROOT / "paired" / device / "results").glob("spikesorted_*"))
    assert len(paths) == 1
    result = check_sorting(si.load(paths[0]))
    (OUT / f"sorting-{device}.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


def workflow_comparison():
    """Compare the two full workflows against the established fixture baseline."""
    sortings = {}
    result = {}
    for device in ("cpu", "gpu"):
        paths = list((ROOT / "runs" / device / "results/postprocessed").glob("*.zarr"))
        assert len(paths) == 1
        sorting = si.load_sorting_analyzer(paths[0]).sorting
        sortings[device] = sorting
        result[device] = check_sorting(sorting)
        assert result[device]["units"] == 15 and result[device]["spikes"] == 40440
        assert result[device]["well_detected_gt_units"] == 15
    comparison = si.compare_two_sorters(sortings["cpu"], sortings["gpu"])
    matched = [(left, right) for left, right in comparison.hungarian_match_12.items() if right >= 0]
    assert len(matched) == 15
    assert all(np.array_equal(sortings["cpu"].get_unit_spike_train(left),
                              sortings["gpu"].get_unit_spike_train(right)) for left, right in matched)
    result["exact_matched_spike_trains"] = len(matched)
    (OUT / "workflow-comparison.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


def analyzers(devices=("cpu", "gpu")):
    """Run in the base image, matching the analyzer extension pickle versions."""
    result = {}
    for device in devices:
        folder = ROOT / "runs" / device / "results"
        analyzers = list((folder / "postprocessed").glob("*.zarr"))
        assert len(analyzers) == 1, f"Missing analyzer: {folder}"
        analyzer = si.load_sorting_analyzer(analyzers[0])
        assert analyzer.recording is not None, "Published analyzer lost its recording reference"
        traces = analyzer.recording.get_traces(start_frame=0, end_frame=300)
        assert traces.shape == (300, FIXTURE["num_channels"]) and np.isfinite(traces).all()
        result[device] = check_sorting(analyzer.sorting)
        metrics = analyzer.get_extension("quality_metrics").get_data()
        required = ["num_spikes", "firing_rate", "presence_ratio", "amplitude_median", "rp_contamination"]
        assert set(required).issubset(metrics.columns)
        assert np.isfinite(metrics[required].to_numpy(dtype=float, na_value=np.nan)).all(), \
            "Required quality metrics are not finite"
        result[device]["metric_nonfinite_counts"] = {
            name: int((~np.isfinite(values.to_numpy(dtype=float, na_value=np.nan))).sum())
            for name, values in metrics.items()}
        reports = list((folder / "reports").glob("*/report_summary.json"))
        assert len(reports) == 1
        summary = json.loads(reports[0].read_text())
        result[device]["report"] = summary
        report_dir = reports[0].parent
        for filename in ("qm_unfiltered.xlsx", "tm_unfiltered.xlsx", "metrics_curated.xlsx", "spike_times.npy"):
            assert (report_dir / filename).stat().st_size > 0
        spike_times = np.load(report_dir / "spike_times.npy", allow_pickle=True).item()
        assert len(spike_times) == summary["n_units_curated"]
        for train in spike_times.values():
            assert np.isfinite(train).all() and (train >= 0).all() and (train < FIXTURE["duration_s"]).all()
        if spike_times:
            for filename in ("waveforms_grid.pdf", "locations_unfiltered.pdf"):
                assert (report_dir / filename).stat().st_size > 0
        bursts = list((folder / "bursts").glob("*/network_results.json"))
        assert len(bursts) == 1
        burst = json.loads(bursts[0].read_text())
        result[device]["bursts"] = {"status": burst["status"], "n_units": burst["n_units"],
                                     "network_bursts": len(burst["network_bursts"]["events"])}
        assert burst["n_units"] == sum(len(v) > 0 for v in spike_times.values())
        if spike_times:
            assert (bursts[0].parent / "raster_burst_plot.png").stat().st_size > 0
    filename = "analyzers.json" if len(devices) == 2 else f"analyzers-{devices[0]}.json"
    (OUT / filename).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


def full(devices=("cpu", "gpu")):
    """Run in the NWB image after analyzer validation; combine both checks."""
    from pynwb import NWBHDF5IO, validate as validate_nwb
    from pynwb.ecephys import LFP
    from hdmf_zarr import NWBZarrIO
    filename = "analyzers.json" if len(devices) == 2 else f"analyzers-{devices[0]}.json"
    result = json.loads((OUT / filename).read_text())
    for device in devices:
        folder = ROOT / "runs" / device / "results"
        nwbs = [p for p in (folder / "nwb").iterdir() if p.name.endswith((".nwb", ".nwb.zarr"))]
        assert len(nwbs) == 1, f"Missing NWB export: {folder}"
        cls = NWBZarrIO if nwbs[0].is_dir() else NWBHDF5IO
        with cls(str(nwbs[0]), mode="r") as io:
            nwb = io.read()
            assert nwb.electrodes is not None and len(nwb.electrodes) == FIXTURE["num_channels"]
            assert nwb.units is not None and len(nwb.units) == result[device]["units"]
            nwb_spikes = 0
            for train in nwb.units["spike_times"]:
                assert np.isfinite(train).all() and (train >= 0).all() and (train < FIXTURE["duration_s"]).all()
                nwb_spikes += len(train)
            assert nwb_spikes == result[device]["spikes"], "NWB export lost spikes"
            lfp_series = [series for module in nwb.processing.values()
                          for interface in module.data_interfaces.values() if isinstance(interface, LFP)
                          for series in interface.electrical_series.values()]
            assert len(lfp_series) == 1, "Missing fixture LFP export"
            lfp = lfp_series[0]
            lfp_rate = 1250.0
            if lfp.timestamps is not None:
                timestamps = np.asarray(lfp.timestamps[:])
                assert len(timestamps) == lfp.data.shape[0] and np.isfinite(timestamps).all()
                # The pinned capsule centers the first 2500-Hz sample at 0.2 ms
                # before decimating to 1250 Hz; the first timestamp is not zero.
                assert 0 <= timestamps[0] < 1 / lfp_rate
                assert timestamps[-1] < FIXTURE["duration_s"]
                assert np.allclose(np.diff(timestamps), 1 / lfp_rate)
                lfp_time_bounds = [float(timestamps[0]), float(timestamps[-1])]
            else:
                assert lfp.rate == lfp_rate
                lfp_time_bounds = [float(lfp.starting_time),
                                   float(lfp.starting_time + (lfp.data.shape[0] - 1) / lfp.rate)]
            assert lfp.data.shape == (int(FIXTURE["duration_s"] * lfp_rate), FIXTURE["num_channels"] // 4)
            assert np.isfinite(lfp.data[:300]).all() and np.isfinite(lfp.data[-300:]).all()
            errors = validate_nwb(io=io)
            assert not errors, f"Invalid NWB: {errors}"
            result[device]["nwb"] = {"file": nwbs[0].name, "units": len(nwb.units),
                                      "spikes": nwb_spikes, "electrodes": len(nwb.electrodes),
                                      "lfp_shape": list(lfp.data.shape), "lfp_rate": lfp_rate,
                                      "lfp_time_bounds_seconds": lfp_time_bounds,
                                      "validation_errors": len(errors)}
    filename = "full.json" if len(devices) == 2 else f"full-{devices[0]}.json"
    (OUT / filename).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("paired", "analyzers", "full", "sorting", "compare-workflows"))
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--workflow-device", choices=("cpu", "gpu"),
                        help="Validate one completed workflow before the other finishes")
    args = parser.parse_args()
    versions = {}
    for package in ("spikeinterface", "pynwb", "hdmf", "hdmf-zarr", "platformdirs", "neuroconv",
                    "numpy", "scipy", "pandas", "zarr"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    (OUT / f"versions-{args.phase}.json").write_text(json.dumps(versions, indent=2))
    if args.phase == "sorting":
        single_sort(args.device)
    elif args.phase == "paired":
        paired()
    elif args.phase == "compare-workflows":
        workflow_comparison()
    else:
        check = analyzers if args.phase == "analyzers" else full
        check((args.workflow_device,) if args.workflow_device else ("cpu", "gpu"))
