#!/usr/bin/env python3
"""Configure the existing generic dispatcher for all wells of a Maxwell file.

Distinct probe names preserve well identity when the NWB exporter groups
electrodes by device. Trace data, geometry, gains and sorter settings are unchanged.
"""
import argparse
import json
from pathlib import Path

import h5py
import probeinterface as pi
import spikeinterface.extractors as se


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-params", type=Path, default=Path(__file__).parents[1] / "params_no_motion.json")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    probe_dir = args.output / "probes"
    probe_dir.mkdir(exist_ok=True)
    with h5py.File(args.input, "r") as handle:
        recordings = {well: list(handle["wells"][well]) for well in handle["wells"]}
    rec_names = {name for values in recordings.values() for name in values}
    if len(rec_names) != 1:
        raise ValueError(f"Expected one recording per well; explicitly handle every recording before running: {recordings}")
    rec_name = next(iter(rec_names))
    streams, _ = se.get_neo_streams("maxwell", file_path=args.input, rec_name=rec_name)
    if set(streams) != set(recordings):
        raise ValueError(f"Reader streams do not cover every well: {streams} vs {recordings}")
    inventory = {}
    probe_paths = {}
    for stream in streams:
        recording = se.read_maxwell(args.input, stream_name=stream, rec_name=rec_name, install_maxwell_plugin=False)
        group = recording.get_probegroup()
        original_names = [probe.annotations.get("name") for probe in group.probes]
        for index, probe in enumerate(group.probes):
            probe.annotate(name=f"{stream}_probe{index}")
        probe_path = probe_dir / f"{stream}.json"
        pi.write_probeinterface(probe_path, group)
        probe_paths[stream] = str(probe_path.resolve())
        inventory[stream] = {
            "frames": [recording.get_num_frames(i) for i in range(recording.get_num_segments())],
            "sample_rate": recording.get_sampling_frequency(), "channels": recording.get_num_channels(),
            "original_probe_names": original_names,
            "probe_names": [probe.annotations["name"] for probe in group.probes],
        }
    settings = json.loads(args.base_params.read_text())
    settings["job_dispatch"] = {
        "input": "spikeinterface", "debug": False, "debug_duration": 60,
        "split_segments": True, "split_groups": True, "multi_session": False,
        "min_recording_duration": -1,
        "spikeinterface_info": {
            "reader_type": "maxwell",
            # Generic stream enumeration accepts only Neo-mappable kwargs.
            # The reader reuses the already-staged plugin without downloading.
            "reader_kwargs": {"file_path": f"../data/ecephys_session/{args.input.name}", "rec_name": rec_name},
            "probe_paths": probe_paths,
            "session_names": args.input.name.removesuffix(".raw.h5"),
        },
    }
    (args.output / "params.json").write_text(json.dumps(settings, indent=2) + "\n")
    result = {"input": str(args.input), "recording": rec_name, "streams": inventory,
              "all_wells_included": True, "full_duration": True}
    (args.output / "inventory.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
