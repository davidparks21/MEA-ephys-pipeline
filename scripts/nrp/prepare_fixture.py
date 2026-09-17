#!/usr/bin/env python3
"""Create deterministic signed/unsigned NWB fixtures and ground truth on NRP."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path


def main():
    import numpy as np
    import spikeinterface as si
    import spikeinterface.extractors as se
    import spikeinterface.preprocessing as spre
    from neuroconv.tools.spikeinterface import add_recording_to_nwbfile
    from pynwb import NWBFile, NWBHDF5IO
    from pynwb.file import Subject

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {"duration_s": 180, "num_channels": 32, "num_units": 20,
                "sampling_frequency": 30000, "seed": 42,
                "spikeinterface": importlib.metadata.version("spikeinterface")}
    recording, sorting = si.generate_ground_truth_recording(
        durations=[manifest["duration_s"]], num_channels=manifest["num_channels"],
        num_units=manifest["num_units"], sampling_frequency=manifest["sampling_frequency"],
        seed=manifest["seed"],
    )
    sorting.save(folder=args.output / "ground_truth")
    signed = spre.astype(recording, dtype="int16")
    signed.set_channel_gains(1.0)
    signed.set_channel_offsets(0.0)
    unsigned = spre.scale(signed, gain=1.0, offset=32768.0, dtype="uint16")
    unsigned.set_channel_gains(1.0)
    unsigned.set_channel_offsets(-32768.0)
    manifest["files"] = {}
    for name, rec in (("signed", signed), ("unsigned", unsigned)):
        folder = args.output / name
        folder.mkdir()
        path = folder / "synthetic.nwb"
        nwb = NWBFile(session_description="Deterministic MEA pipeline smoke test",
                      identifier=f"mea-synthetic-42-{name}",
                      session_start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                      institution="UCSC", lab="Braingeneers")
        nwb.subject = Subject(subject_id="synthetic", description="Generated ground truth")
        add_recording_to_nwbfile(rec, nwbfile=nwb)
        with NWBHDF5IO(path, mode="w") as io:
            io.write(nwb)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        manifest["files"][str(path.relative_to(args.output))] = {
            "sha256": digest.hexdigest(), "bytes": path.stat().st_size, "dtype": str(rec.get_dtype())}
        print(f"Created {name}: {path.stat().st_size} bytes", flush=True)
    signed_read = se.read_nwb_recording(args.output / "signed/synthetic.nwb")
    unsigned_read = se.read_nwb_recording(args.output / "unsigned/synthetic.nwb")
    # Check the whole recording in bounded chunks, including the final sample.
    max_difference = 0.0
    for start in range(0, signed_read.get_num_samples(), 30000):
        end = min(start + 30000, signed_read.get_num_samples())
        left = signed_read.get_traces(start_frame=start, end_frame=end, return_in_uV=True)
        right = unsigned_read.get_traces(start_frame=start, end_frame=end, return_in_uV=True)
        max_difference = max(max_difference, float(np.max(np.abs(left - right))))
    assert max_difference < 0.01, f"NWB representations differ by {max_difference} uV"
    manifest["signed_unsigned_max_difference_uV"] = max_difference
    manifest["ground_truth_spikes"] = int(len(sorting.to_spike_vector()))
    (args.output / "fixture.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
