#!/usr/bin/env python3
"""Read real sample windows and inventory streams before a full workflow run."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import traceback

import h5py
import numpy as np
import spikeinterface.extractors as se


def inspect_recording(recording):
    result = {
        "channels": recording.get_num_channels(),
        "sample_rate": recording.get_sampling_frequency(),
        "dtype": str(recording.get_dtype()),
        "segments": recording.get_num_segments(),
        "channel_ids": recording.get_channel_ids().tolist(),
        "properties": recording.get_property_keys(),
        "probe_info": [probe.annotations for probe in recording.get_probegroup().probes],
        "gain_to_uV": recording.get_channel_gains().tolist(),
        "offset_to_uV": recording.get_channel_offsets().tolist(),
        "windows": [],
    }
    result["locations"] = recording.get_channel_locations().tolist()
    result["frames"] = [recording.get_num_frames(i) for i in range(recording.get_num_segments())]
    for segment, frames in enumerate(result["frames"]):
        count = min(1000, frames)
        for start in sorted({0, max(0, frames // 2 - count // 2), frames - count}):
            data = recording.get_traces(segment_index=segment, start_frame=start, end_frame=start + count)
            scaled = recording.get_traces(segment_index=segment, start_frame=start, end_frame=start + count,
                                           return_in_uV=True)
            assert data.shape == (count, recording.get_num_channels())
            assert np.isfinite(scaled).all(), "Nonfinite scaled sample"
            result["windows"].append({
                "segment": segment, "start": start, "frames": count,
                "minimum": int(data.min()), "maximum": int(data.max()),
                "minimum_uV": float(scaled.min()), "maximum_uV": float(scaled.max()),
                "sha256": hashlib.sha256(data.tobytes()).hexdigest(),
            })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("maxwell", "nwb"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {
        "input": str(args.input), "kind": args.kind,
        "bytes": args.input.stat().st_size,
        "versions": {name: importlib.metadata.version(name) for name in ("h5py", "neo", "spikeinterface")},
        "hdf5_version": h5py.version.hdf5_version,
        "plugin_path": os.environ.get("HDF5_PLUGIN_PATH"),
        "filter401_available_before_reader": bool(h5py.h5z.filter_avail(401)),
        "streams": {}, "errors": [],
    }
    try:
        with h5py.File(args.input, "r") as handle:
            result["root_keys"] = list(handle)
            if args.kind == "maxwell":
                result["bits"] = np.asarray(handle["bits"]).tolist() if "bits" in handle else None
                result["well_recordings"] = {well: list(handle["wells"][well]) for well in handle["wells"]}
            else:
                data = handle["acquisition/ElectricalSeries/data"]
                properties = data.id.get_create_plist()
                result["electrical_series"] = {
                    "shape": data.shape, "dtype": str(data.dtype),
                    "filters": [properties.get_filter(i)[0] for i in range(properties.get_nfilters())],
                }
        if args.kind == "maxwell":
            names, ids = se.get_neo_streams("maxwell", file_path=args.input)
            result["stream_names"] = list(names)
            result["stream_ids"] = list(ids)
            for name in names:
                try:
                    result["streams"][name] = inspect_recording(se.read_maxwell(args.input, stream_name=name))
                except Exception:
                    result["errors"].append({"stream": name, "traceback": traceback.format_exc()})
        else:
            result["streams"]["ElectricalSeries"] = inspect_recording(
                se.read_nwb_recording(args.input, electrical_series_path="acquisition/ElectricalSeries"))
            result["streams"]["ElectricalSeries"]["dispatch_stream"] = "acquisition-ElectricalSeries"
    except Exception:
        result["errors"].append({"traceback": traceback.format_exc()})
    result["filter401_available_after_reader"] = bool(h5py.h5z.filter_avail(401))
    result["passed"] = bool(result["streams"]) and not result["errors"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, default=str) + "\n")
    summary = {k: v for k, v in result.items() if k != "streams"}
    summary["streams"] = {key: {k: v for k, v in value.items() if k not in ("locations", "channel_ids", "gain_to_uV", "offset_to_uV")}
                          for key, value in result["streams"].items()}
    print(json.dumps(summary, indent=2, default=str), flush=True)
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
