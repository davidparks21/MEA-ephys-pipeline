#!/usr/bin/env python3
"""Validate complete real-recording runs without synthetic ground-truth assumptions."""
import argparse
from collections import Counter
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shlex

import numpy as np


def spike_hash(samples):
    return hashlib.sha256(np.asarray(samples, dtype="<i8").tobytes()).hexdigest()


def check_execution(run, streams):
    exit_file = max((run / "evidence").glob("exit-*.json"))
    stamp = exit_file.stem.removeprefix("exit-")
    assert json.loads(exit_file.read_text())["exit_code"] == 0, "Latest driver did not succeed"
    with (run / "evidence" / f"trace-{stamp}.tsv").open() as file:
        rows = list(csv.DictReader(file, delimiter="\t"))
    assert rows and all(row["status"] in ("COMPLETED", "CACHED") and row["exit"] == "0"
                        for row in rows), rows
    counts = Counter(row["name"].split(" (")[0] for row in rows)
    expected = {name: len(streams) for name in (
        "preprocessing", "spikesort_kilosort4", "postprocessing", "curation",
        "visualization", "report_generation", "burst_detection")}
    expected.update({name: 1 for name in ("job_dispatch", "results_collector", "nwb_ecephys", "nwb_units")})
    assert counts == expected, f"Incomplete process coverage: {counts}; expected {expected}"
    command = json.loads((run / "evidence" / f"command-{stamp}.json").read_text())
    profile = command[command.index("-profile") + 1]
    assert profile in ("cpu", "gpu"), f"Unrecognized test profile: {profile}"
    params = json.loads((run / "evidence" / f"params-{stamp}.json").read_text())
    sorter_settings = params.get("spikesorting", {}).get("kilosort4")
    if not sorter_settings:
        pipeline = Path(command[command.index("run") + 1]).parent
        sorter_settings = json.loads((pipeline / "kilosort4_defaults.json").read_text())
    sorter_settings["sorter"]["torch_device"] = "cuda" if profile == "gpu" else "cpu"
    images = json.loads(Path(__file__).with_name("images.json").read_text())
    for row in rows:
        process = row["name"].split(" (")[0]
        image = "ks4" if process == "spikesort_kilosort4" else "nwb" if process.startswith("nwb_") else "base"
        assert row["container"] == images[image], f"Unexpected processing image: {process}"
        prefix, suffix = row["hash"].split("/")
        matches = list((run / "work" / prefix).glob(suffix + "*"))
        assert len(matches) == 1, (row["hash"], matches)
        work = matches[0]
        wrapper = (work / ".command.run").read_text()
        for first, second in (("CO_CPUS", "N_JOBS_EXT"),
                              ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS"),
                              ("MKL_NUM_THREADS", "NUMBA_NUM_THREADS")):
            assert f"export {first}={row['cpus']} {second}={row['cpus']}" in wrapper, \
                f"Missing NRP thread limits: {process}/{first}"
        publications = {
            "report_generation": ("reports", "report_summary.json"),
            "burst_detection": ("bursts", "network_results.json"),
        }
        if process in publications:
            subdir, name = publications[process]
            artifacts = list((work / "capsule/results").glob(f"*/{name}"))
            assert len(artifacts) == 1, (process, artifacts)
            published = run / "results" / subdir / artifacts[0].parent.name / name
            assert hashlib.sha256(artifacts[0].read_bytes()).digest() == hashlib.sha256(published.read_bytes()).digest(), \
                f"Published output differs from latest task: {published}"
        elif process == "nwb_units":
            artifacts = list((work / "capsule/results").glob("*.nwb*"))
            assert len(artifacts) == 1, (process, artifacts)
            artifact = artifacts[0]
            published = run / "results/nwb" / artifact.name
            if artifact.is_dir():
                artifact, published = artifact / ".zmetadata", published / ".zmetadata"
            def digest(path):
                hasher = hashlib.sha256()
                with path.open("rb") as file:
                    for chunk in iter(lambda: file.read(8 * 1024 * 1024), b""):
                        hasher.update(chunk)
                return hasher.digest()
            assert digest(artifact) == digest(published), f"Published NWB differs from latest task: {published}"
        if process == "spikesort_kilosort4":
            desired = sorter_settings
        elif process == "preprocessing" and params.get("preprocessing"):
            desired = params["preprocessing"]
        else:
            continue
        line = next(line.strip() for line in (work / ".command.sh").read_text().splitlines()
                    if line.strip().startswith(("./run --params ", "bash run --params ")))
        tokens = shlex.split(line)
        actual = json.loads(tokens[tokens.index("--params") + 1])
        assert actual == desired, f"Stale task settings in {process}: {work}"
    return {"driver_exit": 0, "trace": str(run / "evidence" / f"trace-{stamp}.tsv"),
            "process_counts": dict(counts), "task_settings_match": True,
            "pinned_images_and_thread_limits": True, "publication_matches_latest_tasks": True,
            "sorter_settings": sorter_settings}


def check_analyzers(run, expected):
    import spikeinterface as si

    result = {"execution": check_execution(run, expected), "streams": {}}
    folder = run / "results"
    paths = list((folder / "postprocessed").glob("*.zarr"))
    assert len(paths) == len(expected), "Not every input stream has a published analyzer"
    for stream, source in expected.items():
        print(f"Validating analyzer, reports and bursts: {stream}", flush=True)
        dispatch_stream = source.get("dispatch_stream", stream)
        matches = [path for path in paths if f"_{dispatch_stream}_" in path.name]
        assert len(matches) == 1, (stream, matches)
        path = matches[0]
        recording_id = path.name.removeprefix("postprocessed_").removesuffix(".zarr")
        analyzer = si.load_sorting_analyzer(path)
        recording, sorting = analyzer.recording, analyzer.sorting
        assert recording is not None, f"Broken published recording reference: {stream}"
        frames = [recording.get_num_frames(i) for i in range(recording.get_num_segments())]
        assert sorting.get_num_segments() == len(frames)
        assert frames == source["frames"], (stream, "Duration changed", frames, source["frames"])
        assert recording.get_sampling_frequency() == source["sample_rate"]
        assert 0 < recording.get_num_channels() <= source["channels"]
        for segment, count in enumerate(frames):
            for start in (0, max(0, count - 300)):
                traces = recording.get_traces(segment_index=segment, start_frame=start, end_frame=min(count, start + 300))
                assert np.isfinite(traces).all() and traces.shape[1] == recording.get_num_channels()
        spikes = sorting.to_spike_vector()
        for segment, count in enumerate(frames):
            selected = spikes[spikes["segment_index"] == segment]["sample_index"]
            assert ((selected >= 0) & (selected < count)).all()
        assert len(frames) == 1, "Extend per-segment NWB validation before claiming multi-segment coverage"
        metrics = analyzer.get_extension("quality_metrics").get_data()
        assert len(metrics) == sorting.get_num_units()
        required = ["num_spikes", "firing_rate", "presence_ratio"]
        assert set(required).issubset(metrics.columns)
        assert np.isfinite(metrics[required].to_numpy(dtype=float, na_value=np.nan)).all()
        assert int(metrics["num_spikes"].sum()) == len(spikes)
        report_dir = folder / "reports" / recording_id
        summary = json.loads((report_dir / "report_summary.json").read_text())
        assert summary["n_units_total"] == sorting.get_num_units()
        report_spikes = np.load(report_dir / "spike_times.npy", allow_pickle=True).item()
        assert len(report_spikes) == summary["n_units_curated"]
        for unit, times in report_spikes.items():
            expected_times = sorting.get_unit_spike_train(unit) / source["sample_rate"]
            assert np.array_equal(times, expected_times), (stream, unit, "Report changed spike times")
        for name in ("qm_unfiltered.xlsx", "tm_unfiltered.xlsx", "metrics_curated.xlsx"):
            assert (report_dir / name).stat().st_size > 0
        burst_dir = folder / "bursts" / recording_id
        burst = json.loads((burst_dir / "network_results.json").read_text())
        assert burst["n_units"] == sum(len(train) > 0 for train in report_spikes.values())
        if report_spikes:
            assert (report_dir / "waveforms_grid.pdf").stat().st_size > 0
        if burst["n_units"]:
            assert burst["status"] == "ok"
            assert (burst_dir / "raster_burst_plot.png").stat().st_size > 0
        else:
            assert burst["status"] == "no_spikes"
            assert all(not burst[level]["events"] for level in ("burstlets", "network_bursts", "superbursts"))
        if not report_spikes:
            expected_status = "no_detected_units" if sorting.get_num_units() == 0 else "no_curated_units"
            assert summary["status"] == expected_status
        empty_status = None
        if sorting.get_num_units() == 0:
            assert len(spikes) == 0
            sorted_folder = folder / "spikesorted" / recording_id
            empty_status = json.loads((sorted_folder / "sorting_status.json").read_text())
            assert empty_status["status"] == "no_spikes_detected" and empty_status["frames"] == frames
            assert empty_status["units"] == 0 and empty_status["spikes"] == 0
            detector_log = json.loads((sorted_folder / "spikeinterface_log.json").read_text())
            assert detector_log["error_trace"][-1].strip() == "ValueError: No spikes detected, cannot continue sorting."
            assert any("st0 shape: (0, 6)" in line for line in detector_log["runtime_trace"])
        result["streams"][stream] = {
            "analyzer": str(path), "frames": frames, "sample_rate": source["sample_rate"],
            "input_channels": source["channels"], "processed_channels": recording.get_num_channels(),
            "units": sorting.get_num_units(), "spikes": len(spikes),
            "empty_detection_status": empty_status,
            "unit_spike_hashes": {str(unit): spike_hash(sorting.get_unit_spike_train(unit)) for unit in sorting.unit_ids},
            "metric_nonfinite_counts": {name: int((~np.isfinite(values.to_numpy(dtype=float, na_value=np.nan))).sum())
                                        for name, values in metrics.items()},
            "report": summary, "burst_status": burst["status"],
            "network_bursts": len(burst["network_bursts"]["events"]),
        }
    assert sum(item["spikes"] for item in result["streams"].values()) > 0, "No spikes recovered from the dataset"
    return result


def check_nwb(run, expected, result):
    from pynwb import NWBHDF5IO, validate
    from pynwb.ecephys import LFP
    from hdmf_zarr import NWBZarrIO

    paths = [p for p in (run / "results/nwb").iterdir() if p.name.endswith((".nwb", ".nwb.zarr"))]
    assert len(paths) == 1, "Expected one full NWB export containing all streams"
    cls = NWBZarrIO if paths[0].is_dir() else NWBHDF5IO
    with cls(str(paths[0]), mode="r") as io:
        nwb = io.read()
        assert len(nwb.electrodes) == sum(item["channels"] for item in expected.values()), "NWB lost electrodes/well identity"
        electrode_devices = Counter(group.device.name for group in nwb.electrodes["group"][:])
        if len(expected) > 1:
            assert all(len(item["probe_names"]) == 1 for item in expected.values())
            assert electrode_devices == {item["probe_names"][0]: item["channels"] for item in expected.values()}
        assert nwb.units is not None and len(nwb.units) == sum(item["units"] for item in result["streams"].values())
        assert "device_name" in nwb.units.colnames and "ks_unit_id" in nwb.units.colnames
        # The pinned exporter omits waveform/electrode columns for combined
        # streams with unequal channel counts. Check their mapping when present.
        waveform_columns = [name for name in ("waveform_mean", "waveform_sd", "electrodes")
                            if name in nwb.units.colnames]
        actual_hashes = {stream: {} for stream in expected}
        spike_total = 0
        for row in range(len(nwb.units)):
            device = str(nwb.units["device_name"][row])
            if len(expected) == 1:
                stream = next(iter(expected))
            else:
                choices = [key for key, item in expected.items() if device in item.get("probe_names", [])]
                assert len(choices) == 1, f"Cannot assign exported unit to a well: {device}"
                stream = choices[0]
            source = expected[stream]
            if "electrodes" in nwb.units.colnames:
                electrodes = nwb.units["electrodes"][row]
                assert len(electrodes) > 0
                assert all(group.device.name == device for group in electrodes["group"]), \
                    f"Unit electrode pointers cross devices: {stream}"
            train = np.asarray(nwb.units["spike_times"][row])
            assert np.isfinite(train).all() and (train >= 0).all()
            assert (train < source["frames"][0] / source["sample_rate"]).all()
            samples = np.rint(train * source["sample_rate"]).astype("int64")
            unit = str(nwb.units["ks_unit_id"][row])
            assert unit not in actual_hashes[stream], "Duplicate unit identity within a well"
            actual_hashes[stream][unit] = spike_hash(samples)
            spike_total += len(train)
        for stream in expected:
            assert actual_hashes[stream] == result["streams"][stream]["unit_spike_hashes"], f"NWB changed spikes for {stream}"
        series = [series for module in nwb.processing.values()
                  for interface in module.data_interfaces.values() if isinstance(interface, LFP)
                  for series in interface.electrical_series.values()]
        assert len(series) == len(expected), "NWB lost an LFP stream"
        lfp_results = {}
        for lfp in series:
            if len(expected) == 1:
                stream = next(iter(expected))
            else:
                choices = [key for key, item in expected.items() if any(name in lfp.name for name in item["probe_names"])]
                assert len(choices) == 1, f"Cannot identify LFP well: {lfp.name}"
                stream = choices[0]
            assert stream not in lfp_results
            source = expected[stream]
            duration = source["frames"][0] / source["sample_rate"]
            assert lfp.data.shape == (round(duration * 1250), math.ceil(source["channels"] / 4)), (stream, lfp.data.shape)
            device = source["probe_names"][0] if len(expected) > 1 else next(iter(electrode_devices))
            indices = [index for index, group in enumerate(nwb.electrodes["group"][:])
                       if group.device.name == device]
            assert np.array_equal(lfp.electrodes.data[:], indices[::4]), \
                f"LFP electrode pointers changed channel/well identity: {stream}"
            assert np.isfinite(lfp.data[:300]).all() and np.isfinite(lfp.data[-300:]).all()
            if lfp.timestamps is not None:
                times = np.asarray(lfp.timestamps[:])
                assert len(times) == lfp.data.shape[0] and np.isfinite(times).all()
                assert 0 <= times[0] < 1 / 1250 and times[-1] < duration
                assert np.allclose(np.diff(times), 1 / 1250)
            else:
                assert lfp.rate == 1250 and 0 <= lfp.starting_time < 1 / 1250
            lfp_results[stream] = {"series": lfp.name, "shape": list(lfp.data.shape), "rate": 1250}
        errors = validate(io=io)
        assert not errors, [str(error) for error in errors]
        result["nwb"] = {"path": str(paths[0]), "electrodes": len(nwb.electrodes), "units": len(nwb.units),
                         "electrodes_by_device": dict(electrode_devices),
                         "unit_waveform_columns": waveform_columns,
                         "spikes": spike_total, "lfp": lfp_results, "schema_errors": []}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("analyzers", "nwb"))
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args()
    root = Path(os.environ["TEST_ROOT"])
    run = root / "runs" / args.run_name
    output = root / "validation"
    output.mkdir(exist_ok=True)
    inventory = json.loads(args.inventory.read_text())
    expected = inventory["streams"]
    # Inspection reports created before dispatch_stream was recorded still have
    # an unambiguous full ElectricalSeries path for this supported NWB input.
    if inventory.get("kind") == "nwb" and set(expected) == {"ElectricalSeries"}:
        expected["ElectricalSeries"].setdefault("dispatch_stream", "acquisition-ElectricalSeries")
    analyzer_result = output / f"real-analyzers-{args.run_name}.json"
    if args.phase == "analyzers":
        result = check_analyzers(run, expected)
        target = analyzer_result
    else:
        result = check_nwb(run, expected, json.loads(analyzer_result.read_text()))
        target = output / f"real-full-{args.run_name}.json"
    target.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
