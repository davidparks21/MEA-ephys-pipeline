#!/usr/bin/env python3
import os
import sys
import json
import traceback
import argparse
import logging
from pathlib import Path
from datetime import datetime

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks


def setup_logger(output_dir):
    log_file = Path(output_dir) / "burst_detection.log"
    logger = logging.getLogger("burst_detection")
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s')
    fh = logging.FileHandler(log_file, mode='w')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger


def compute_network_bursts(SpikeTimes=None, gamma=1.0, network_merge_gap_min=0.75, verbose=True):
    units = list(SpikeTimes.keys())
    if not units:
        return {"error": "no_units"}
    all_spikes = np.sort(np.concatenate([SpikeTimes[u] for u in units if len(SpikeTimes[u]) > 0]))
    if all_spikes.size == 0:
        return {"error": "no_spikes"}
    rec_start = float(all_spikes[0])
    rec_end   = float(all_spikes[-1])
    total_dur = rec_end - rec_start

    all_log_isis = []
    for u in units:
        t = np.unique(np.sort(SpikeTimes[u]))
        if len(t) >= 2:
            isi = np.diff(t)
            isi = isi[isi > 0]
            if isi.size > 0:
                all_log_isis.extend(np.log10(isi))
    biological_isi_s = 10 ** np.median(all_log_isis) if all_log_isis else 0.1
    adaptive_bin_ms  = np.clip(biological_isi_s * 1000, 10, 30)
    bin_size         = adaptive_bin_ms / 1000.0
    bins      = np.arange(rec_start, rec_end + bin_size, bin_size)
    t_centers = (bins[:-1] + bins[1:]) / 2
    n_bins  = len(t_centers)
    n_units = len(units)
    active_unit_counts = np.zeros(n_bins)
    spike_counts_total = np.zeros(n_bins)
    for u in units:
        spk = np.asarray(SpikeTimes[u])
        if spk.size == 0:
            continue
        counts, _ = np.histogram(spk, bins=bins)
        active_unit_counts += (counts > 0)
        spike_counts_total += counts
    participation_signal_raw = active_unit_counts / max(1, n_units)
    rate_signal_raw          = spike_counts_total / bin_size / max(1, n_units)
    PFR                      = spike_counts_total / bin_size
    isi_bins   = biological_isi_s / bin_size
    sigma_fast = np.clip(isi_bins, 1, 2)
    sigma_slow = np.clip(5.0 * isi_bins, 3, 8)
    ws_sharp  = gaussian_filter1d(participation_signal_raw, sigma_fast)
    ws_smooth = gaussian_filter1d(rate_signal_raw, sigma_slow)
    burstlet_merge_gap_s = 3 * biological_isi_s
    network_merge_gap_s  = max(10 * biological_isi_s, network_merge_gap_min)
    participation_floor_count = max(5, 0.15 * n_units) if n_units < 50 else max(10, 0.05 * n_units)
    participation_floor       = participation_floor_count / max(1, n_units)
    baseline_val           = np.median(ws_sharp)
    spread_mad             = np.median(np.abs(ws_sharp - baseline_val))
    relative_threshold_val = max(participation_floor, baseline_val + 0.75 * spread_mad)
    min_prominence = max(0.5 * spread_mad, 0.02)
    peaks, _ = find_peaks(ws_sharp, height=relative_threshold_val, prominence=min_prominence)

    extent_frac = 0.30
    burstlets = []
    for p in peaks:
        peak_val         = ws_sharp[p]
        extent_threshold = max(relative_threshold_val, extent_frac * peak_val)
        s = p
        while s > 0 and ws_sharp[s - 1] >= extent_threshold:
            s -= 1
        e = p
        while e < n_bins - 1 and ws_sharp[e + 1] >= extent_threshold:
            e += 1
        start_t    = bins[s]
        end_t      = bins[e + 1]
        duration_s = end_t - start_t
        if duration_s <= 0:
            continue
        participating = sum(1 for u in units if np.any((SpikeTimes[u] >= start_t) & (SpikeTimes[u] < end_t)))
        participation_frac = participating / n_units
        total_spikes       = int(np.sum(spike_counts_total[s:e + 1]))
        burstlets.append({
            "start": float(start_t), "end": float(end_t), "duration_s": float(duration_s),
            "peak_synchrony": float(peak_val), "peak_time": float(t_centers[p]),
            "synchrony_energy": float(np.sum(ws_smooth[s:e + 1]) * bin_size),
            "participation": participation_frac, "total_spikes": total_spikes,
            "burst_peak": float(np.max(PFR[s:e + 1]))
        })

    def get_valley_min(prev, nxt):
        valley_mask = (t_centers >= prev["end"]) & (t_centers <= nxt["start"])
        if not np.any(valley_mask):
            return None
        valley_vals = ws_sharp[valley_mask]
        return float(np.min(valley_vals)) if valley_vals.size > 0 else None

    def finalize(evs, s, e):
        best = max(evs, key=lambda x: x["peak_synchrony"])
        participating_units = sum(1 for u in units if np.any((SpikeTimes[u] >= s) & (SpikeTimes[u] < e)))
        return {
            "start": s, "end": e, "duration_s": e - s,
            "peak_synchrony": best["peak_synchrony"], "peak_time": best["peak_time"],
            "synchrony_energy": sum(ev["synchrony_energy"] for ev in evs),
            "fragment_count": sum(ev.get("fragment_count", 1) for ev in evs),
            "total_spikes": sum(ev["total_spikes"] for ev in evs),
            "participation": participating_units / n_units,
            "burst_peak": max(ev["burst_peak"] for ev in evs),
            "n_sub_events": len(evs)
        }

    def merge_strict(events, gap, floor_val):
        if not events:
            return []
        events = sorted(events, key=lambda x: x["start"])
        merged, curr = [], [events[0]]
        s, e = events[0]["start"], events[0]["end"]
        for nxt in events[1:]:
            valley_duration = nxt["start"] - e
            valley_min      = get_valley_min(curr[-1], nxt)
            valley_ok = (valley_min is None and valley_duration <= bin_size) or \
                        (valley_min is not None and valley_min >= floor_val)
            if valley_duration <= gap and valley_ok:
                curr.append(nxt); e = max(e, nxt["end"])
            else:
                merged.append(finalize(curr, s, e)); curr, s, e = [nxt], nxt["start"], nxt["end"]
        merged.append(finalize(curr, s, e))
        return merged

    def merge_clustered(events, gap, baseline_val, threshold_val):
        if not events:
            return []
        events = sorted(events, key=lambda x: x["start"])
        merged, curr = [], [events[0]]
        s, e = events[0]["start"], events[0]["end"]
        for nxt in events[1:]:
            valley_duration = nxt["start"] - e
            valley_min      = get_valley_min(curr[-1], nxt)
            valley_ok = (valley_min is None and valley_duration <= bin_size) or \
                        (valley_min is not None and valley_min > baseline_val and valley_min < threshold_val)
            if valley_duration <= gap and valley_ok:
                curr.append(nxt); e = max(e, nxt["end"])
            else:
                merged.append(finalize(curr, s, e)); curr, s, e = [nxt], nxt["start"], nxt["end"]
        merged.append(finalize(curr, s, e))
        return [m for m in merged if m["n_sub_events"] >= 2]

    network_bursts = merge_strict(burstlets, burstlet_merge_gap_s, relative_threshold_val)
    superbursts    = merge_clustered(network_bursts, network_merge_gap_s, baseline_val, relative_threshold_val)

    def stats(x):
        x = np.asarray(x)
        if x.size == 0:
            return {"mean": 0.0, "std": 0.0, "cv": 0.0}
        mean_val = x.mean(); std_val = x.std()
        return {"mean": float(mean_val), "std": float(std_val),
                "cv": float(std_val / mean_val) if abs(mean_val) > 1e-12 else float('nan')}

    def level_metrics(events):
        if not events:
            return {}
        starts = [ev["start"] for ev in events]
        return {
            "count": len(events), "rate": len(events) / total_dur,
            "duration":            stats([ev["duration_s"]       for ev in events]),
            "inter_event_interval": stats(np.diff(starts)) if len(starts) > 1 else stats([]),
            "intensity":           stats([ev["synchrony_energy"] for ev in events]),
            "participation":       stats([ev["participation"]    for ev in events]),
            "spikes_per_burst":    stats([ev["total_spikes"]     for ev in events]),
            "burst_peak":          stats([ev["burst_peak"]       for ev in events]),
            "peak_synchrony":      stats([ev["peak_synchrony"]   for ev in events]),
        }

    return {
        "burstlets":      {"events": burstlets,      "metrics": level_metrics(burstlets)},
        "network_bursts": {"events": network_bursts, "metrics": level_metrics(network_bursts)},
        "superbursts":    {"events": superbursts,    "metrics": level_metrics(superbursts)},
        "diagnostics": {
            "adaptive_bin_ms": adaptive_bin_ms, "biological_isi_s": biological_isi_s,
            "baseline_value": baseline_val, "spread_mad": spread_mad,
            "merge_floor": relative_threshold_val,
            "burstlet_merge_gap_s": burstlet_merge_gap_s,
            "network_merge_gap_s": network_merge_gap_s, "n_units": n_units,
        },
        "plot_data": {
            "t": t_centers, "participation_signal": ws_sharp, "rate_signal": ws_smooth,
            "burst_peak_times":  np.array([b["peak_time"]      for b in network_bursts]),
            "burst_peak_values": np.array([b["peak_synchrony"] for b in network_bursts]),
            "participation_baseline":  baseline_val,
            "participation_threshold": relative_threshold_val,
        }
    }


class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer): return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        return super().default(obj)

def recursive_clean(obj):
    if isinstance(obj, dict):   return {k: recursive_clean(v) for k, v in obj.items()}
    if isinstance(obj, list):   return [recursive_clean(v) for v in obj]
    if isinstance(obj, np.ndarray): return obj.tolist()
    if isinstance(obj, np.integer): return int(obj)
    if isinstance(obj, np.floating): return float(obj)
    return obj

def plot_clean_raster(ax, spike_times, sorted_units=None, color="gray", markersize=4, alpha=1.0):
    units = sorted_units if sorted_units is not None else list(spike_times.keys())
    for y_idx, uid in enumerate(units):
        spk = spike_times[uid]
        if len(spk) > 0:
            ax.scatter(spk, [y_idx] * len(spk), c=color, s=markersize, alpha=alpha, marker='|')
    ax.set_ylabel("Unit #")
    ax.set_xlim(left=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

def plot_clean_network(ax, t, participation_signal, rate_signal, burst_peak_times,
                       burst_peak_values, participation_baseline, participation_threshold, use_twinx=True):
    ax.plot(t, participation_signal, color="steelblue", lw=1.0)
    ax.axhline(participation_baseline,  color="gray",   lw=0.8, linestyle="--", alpha=0.6)
    ax.axhline(participation_threshold, color="orange", lw=0.8, linestyle="--", alpha=0.8)
    ax.set_ylabel("Participation")
    ax.spines["top"].set_visible(False)
    ax_red = ax
    if use_twinx:
        ax_red = ax.twinx()
        ax_red.plot(t, rate_signal, color="tomato", lw=0.8, alpha=0.6)
        ax_red.set_ylabel("Pop. rate (Hz/unit)")
    if len(burst_peak_times) > 0:
        ax.scatter(burst_peak_times, burst_peak_values, c="red", s=20, zorder=5)
    return ax, ax_red

def mark_burst_hierarchy(ax_raster, ax_network, burstlets, network_bursts, superbursts,
                          show_burstlet_ticks=True, show_network_ticks=True,
                          show_superburst_bars=True, min_superburst_duration_s=2.5):
    if show_burstlet_ticks:
        for b in burstlets:
            ax_raster.axvline(b["peak_time"], color="black", lw=0.6, alpha=0.5, ymin=0.92, ymax=1.0)
    if show_network_ticks:
        for nb in network_bursts:
            ax_raster.axvline(nb["peak_time"], color="steelblue", lw=1.5, alpha=0.8, ymin=0.85, ymax=1.0)
    if show_superburst_bars:
        for sb in superbursts:
            if sb["duration_s"] >= min_superburst_duration_s:
                ax_raster.axvspan(sb["start"], sb["end"], color="mediumpurple", alpha=0.08)


def main():
    parser = argparse.ArgumentParser(description="Burst Detection Capsule")
    parser.add_argument("--spike-times",           type=str,   required=True)
    parser.add_argument("--output-dir",            type=str,   required=True)
    parser.add_argument("--plot-mode",             type=str,   default="separate", choices=["separate", "merged"])
    parser.add_argument("--network-merge-gap-min", type=float, default=0.75)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(output_dir)
    logger.info("=== BURST DETECTION CAPSULE ===")

    logger.info("Loading spike times...")
    try:
        spike_times_raw = np.load(args.spike_times, allow_pickle=True).item()
    except Exception as e:
        logger.error(f"Failed to load spike times: {e}"); sys.exit(1)

    spike_times = {uid: np.asarray(spk, dtype=float)
                   for uid, spk in spike_times_raw.items()
                   if np.asarray(spk).size > 0}
    logger.info(f"Loaded {len(spike_times)} units")

    if not spike_times:
        result = {
            "status": "no_spikes", "n_units": 0,
            "burstlets": {"events": []}, "network_bursts": {"events": []},
            "superbursts": {"events": []},
            "timestamp": str(datetime.now()),
        }
        (output_dir / "network_results.json").write_text(json.dumps(result, indent=2))
        logger.info("No spikes survived report curation; saved an empty burst result.")
        return

    logger.info("Running burst detector...")
    try:
        network_data = compute_network_bursts(SpikeTimes=spike_times,
                                              network_merge_gap_min=args.network_merge_gap_min)
    except Exception as e:
        logger.error(f"Burst detection failed: {e}"); traceback.print_exc(); sys.exit(1)

    if isinstance(network_data, dict) and "error" in network_data:
        logger.error(f"Burst detector error: {network_data['error']}"); sys.exit(1)

    n_b = len(network_data["burstlets"]["events"])
    n_n = len(network_data["network_bursts"]["events"])
    n_s = len(network_data["superbursts"]["events"])
    logger.info(f"Detected: {n_b} burstlets | {n_n} network bursts | {n_s} superbursts")

    clean_data = recursive_clean(network_data)
    clean_data["n_units"] = len(spike_times)
    clean_data["status"] = "ok"
    clean_data["timestamp"] = str(datetime.now())
    temp_file = output_dir / "network_results.tmp.json"
    final_file = output_dir / "network_results.json"
    with open(temp_file, "w") as f:
        json.dump(clean_data, f, indent=2, cls=NpEncoder)
    os.replace(temp_file, final_file)
    logger.info(f"Saved: {final_file}")

    logger.info("Generating plots...")
    try:
        if args.plot_mode == "separate":
            fig, (ax_raster, ax_network) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
            plot_clean_raster(ax_raster, spike_times)
            ax_network, ax_red = plot_clean_network(ax_network, **network_data["plot_data"], use_twinx=True)
        else:
            fig, ax_raster = plt.subplots(figsize=(12, 5))
            plot_clean_raster(ax_raster, spike_times)
            ax_network = ax_raster.twinx()
            ax_network, ax_red = plot_clean_network(ax_network, **network_data["plot_data"], use_twinx=False)

        mark_burst_hierarchy(ax_raster, ax_network,
                              network_data["burstlets"]["events"],
                              network_data["network_bursts"]["events"],
                              network_data["superbursts"]["events"])

        handles = [
            Line2D([0], [0], color="black",        lw=1.2, label="Burstlet ticks"),
            Line2D([0], [0], color="steelblue",    lw=2.0, label="Network burst ticks"),
            Line2D([0], [0], color="mediumpurple", lw=2.2, label="Superbursts"),
            Line2D([0], [0], marker='o', color='red', lw=0, markersize=5, label="Burst centers"),
        ]
        ax_raster.legend(handles=handles, loc="upper right", frameon=False, fontsize=8)
        plt.tight_layout()
        if args.plot_mode == "separate":
            plt.subplots_adjust(hspace=0.05)

        plt.savefig(output_dir / "raster_burst_plot.svg")
        plt.savefig(output_dir / "raster_burst_plot.png", dpi=300)
        ax_raster.set_xlim(0, 60); ax_network.set_xlim(0, 60)
        plt.savefig(output_dir / "raster_burst_plot_60s.svg")
        ax_raster.set_xlim(0, 30); ax_network.set_xlim(0, 30)
        ax_network.set_xlabel("Time (s)")
        plt.savefig(output_dir / "raster_burst_plot_30s.svg")
        plt.savefig(output_dir / "raster_burst_plot_30s.png", dpi=300)
        plt.close(fig)
        logger.info("Plots saved.")
    except Exception as e:
        logger.error(f"Plotting failed: {e}"); traceback.print_exc(); sys.exit(1)

    logger.info("=== BURST DETECTION COMPLETE ===")
    logger.info(f"  Burstlets:      {n_b}")
    logger.info(f"  Network bursts: {n_n}")
    logger.info(f"  Superbursts:    {n_s}")


if __name__ == "__main__":
    main()
