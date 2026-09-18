#!/usr/bin/env python3
"""Check fractional-second LFP tails in the pinned export environment."""
import argparse
import json
from pathlib import Path

import numpy as np
import spikeinterface as si
import spikeinterface.extractors as se
import spikeinterface.preprocessing as spre


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("maxwell", "nwb"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stream", default="well000", help="Maxwell stream to check")
    parser.add_argument("--electrical-series", default="acquisition/ElectricalSeries")
    parser.add_argument("--baseline", type=Path, help="Optional saved, channel-selected tail for exact comparison")
    parser.add_argument("--select-before-save", action="store_true", help="Reproduce the original selection order")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    raw = (se.read_maxwell(args.input, stream_name=args.stream) if args.kind == "maxwell"
           else se.read_nwb_recording(args.input, electrical_series_path=args.electrical_series))
    si.set_global_job_kwargs(n_jobs=2, chunk_duration="1s", progress_bar=False, mp_context="spawn")
    assert raw.get_num_segments() == 1, "This regression checks one segment"
    recording = spre.unsigned_to_signed(raw) if np.issubdtype(raw.get_dtype(), np.unsignedinteger) else raw
    lfp = spre.bandpass_filter(recording, freq_min=0.5, freq_max=500, margin_ms=2000)
    lfp = spre.resample(lfp, 2500)
    lfp = spre.astype(lfp, dtype="int16")
    channels = lfp.channel_ids[::4]
    original_order = spre.highpass_filter(spre.decimate(lfp.select_channels(channels), 2), freq_min=0.1)
    if args.select_before_save:
        lfp = lfp.select_channels(channels)
    lfp = spre.decimate(lfp, 2)
    lfp = spre.highpass_filter(lfp, freq_min=0.1)
    frames = lfp.get_num_frames()
    expected_frames = (int(raw.get_num_frames() / raw.get_sampling_frequency() * 2500) + 1) // 2
    assert frames == expected_frames
    # Keep the final full second and fractional second, aligned to the
    # exporter's 1-second chunks. Filtering is only approximately invariant
    # to chunk boundaries; compare identical windows for the binary check.
    tail_start = max(0, (frames // 1250 - 1) * 1250)
    tail = lfp.frame_slice(start_frame=tail_start, end_frame=frames)
    direct = np.concatenate([lfp.get_traces(start_frame=start, end_frame=min(start + 1250, frames))
                             for start in range(tail_start, frames, 1250)])
    saved = tail.save(folder=args.output / "tail", overwrite=True)
    if not args.select_before_save:
        saved = saved.select_channels(channels)
        direct = direct[:, ::4]
    actual = saved.get_traces()
    assert actual.shape == direct.shape == (frames - tail_start, len(raw.channel_ids[::4]))
    assert np.isfinite(actual).all()
    assert np.array_equal(actual, direct)
    # Compare individual first/middle/last exported channels against the old
    # filter order without issuing its expensive multi-channel HDF5 selection.
    checked_channels = sorted({0, len(channels) // 2, len(channels) - 1})
    for index in checked_channels:
        baseline = np.concatenate([
            original_order.get_traces(start_frame=start, end_frame=min(start + 1250, frames),
                                      channel_ids=[channels[index]])
            for start in range(tail_start, frames, 1250)
        ])
        assert np.array_equal(actual[:, index:index + 1], baseline), \
            f"Filter order changed channel {channels[index]}"
    if args.baseline:
        baseline = si.load(args.baseline)
        assert np.array_equal(actual, baseline.get_traces()), "Reordering changed selected LFP samples"
    result = {"input": str(args.input), "kind": args.kind,
              "input_frames": raw.get_num_frames(), "input_rate": raw.get_sampling_frequency(),
              "lfp_frames": frames, "tail_shape": list(actual.shape),
              "original_order_channel_checks": len(checked_channels), "passed": True}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
