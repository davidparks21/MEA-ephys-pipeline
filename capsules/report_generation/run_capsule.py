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
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.backends.backend_pdf as pdf

import spikeinterface.full as si

DEFAULT_THRESHOLDS = {
    'presence_ratio': 0.75,
    'rp_contamination': 0.15,
    'firing_rate': 0.05,
    'amplitude_median': -20,
}


def setup_logger(output_dir):
    log_file = Path(output_dir) / "report_generation.log"
    logger = logging.getLogger("report_generation")
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s')
    fh = logging.FileHandler(log_file, mode='w')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger


def apply_curation_logic(metrics, user_thresholds=None, logger=None):
    defaults = DEFAULT_THRESHOLDS.copy()
    if user_thresholds:
        defaults.update(user_thresholds)

    keep_mask  = []
    rejections = []
    for idx, row in metrics.iterrows():
        reasons = []
        if row.get('presence_ratio',   1)    < defaults['presence_ratio']:   reasons.append("Low Presence")
        if row.get('rp_contamination', 0)    > defaults['rp_contamination']: reasons.append("High Contam")
        if row.get('firing_rate',      0)    < defaults['firing_rate']:      reasons.append("Low FR")
        if row.get('amplitude_median', -100) > defaults['amplitude_median']: reasons.append("Low Amp")
        keep = len(reasons) == 0
        keep_mask.append(keep)
        if not keep:
            rejections.append({"unit_id": idx, "reasons": "; ".join(reasons)})

    if logger:
        logger.info(f"Curation: {sum(keep_mask)}/{len(metrics)} units passed")
    return metrics.loc[np.asarray(keep_mask, dtype=bool)], pd.DataFrame(rejections)


def plot_probe_locations(recording, unit_ids, locations, filename, output_dir, logger):
    try:
        fig, ax = plt.subplots(figsize=(10.5, 6.5))
        si.plot_probe_map(recording, ax=ax, with_channel_ids=False)
        ax.scatter(locations[:, 0], locations[:, 1], s=10, c='blue', alpha=0.6)
        ax.invert_yaxis()
        ax.set_title(f"Unit locations (n={len(unit_ids)})")
        fig.savefig(output_dir / filename)
        plt.close(fig)
        logger.info(f"Saved: {filename}")
    except Exception as e:
        logger.warning(f"Probe location plot failed: {e}")


def plot_waveforms_grid(analyzer, unit_ids, output_dir, logger):
    pdf_path = output_dir / "waveforms_grid.pdf"
    logger.info(f"Generating waveforms grid: {pdf_path}")
    try:
        wf_ext = analyzer.get_extension("waveforms")
        fs     = analyzer.recording.get_sampling_frequency()
        with pdf.PdfPages(pdf_path) as pdf_doc:
            units_per_page = 12
            for i in range(0, len(unit_ids), units_per_page):
                batch = unit_ids[i: i + units_per_page]
                fig, axes = plt.subplots(3, 4, figsize=(12, 9))
                axes = axes.flatten()
                for ax, uid in zip(axes, batch):
                    try:
                        wf      = wf_ext.get_waveforms_one_unit(uid)
                        mean_wf = np.mean(wf, axis=0)
                        best_ch = np.argmin(np.min(mean_wf, axis=0))
                        time_ms = np.arange(wf.shape[1]) / fs * 1000
                        n_spikes = wf.shape[0]
                        if n_spikes > 100:
                            indices        = np.random.choice(n_spikes, 100, replace=False)
                            spikes_to_plot = wf[indices, :, best_ch]
                        else:
                            spikes_to_plot = wf[:, :, best_ch]
                        ax.plot(time_ms, spikes_to_plot.T, c='gray', lw=0.5, alpha=0.3)
                        ax.plot(time_ms, mean_wf[:, best_ch], c='red', lw=1.5)
                        ax.set_title(f"Unit {uid} | Ch {best_ch}", fontsize=9)
                        ax.spines['top'].set_visible(False)
                        ax.spines['right'].set_visible(False)
                    except Exception as e:
                        ax.set_title(f"Unit {uid} — ERROR", fontsize=8)
                        ax.axis('off')
                for j in range(len(batch), len(axes)):
                    axes[j].axis('off')
                pdf_doc.savefig(fig)
                plt.close(fig)
        logger.info(f"Saved: {pdf_path}")
    except Exception as e:
        logger.error(f"Waveforms grid failed: {e}")
        traceback.print_exc()


def main():
    parser = argparse.ArgumentParser(description="Report Generation Capsule")
    parser.add_argument("--analyzer-dir",  type=str, required=True,
                        help="Path to SpikeInterface analyzer_output folder")
    parser.add_argument("--output-dir",    type=str, required=True,
                        help="Output directory for reports")
    parser.add_argument("--no-curation",   action="store_true",
                        help="Skip curation and use all units")
    parser.add_argument("--thresholds",    type=str, default=None,
                        help="JSON string with curation thresholds")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(output_dir)
    logger.info("=== REPORT GENERATION CAPSULE ===")
    logger.info(f"Analyzer dir: {args.analyzer_dir}")

    user_thresholds = None
    if args.thresholds:
        try:
            user_thresholds = json.loads(args.thresholds)
            logger.info(f"Custom thresholds: {user_thresholds}")
        except Exception as e:
            logger.warning(f"Could not parse thresholds: {e}")

    logger.info("Loading SpikeInterface analyzer...")
    try:
        analyzer = si.load_sorting_analyzer(args.analyzer_dir, format="zarr")
        sorting  = analyzer.sorting
        logger.info(f"Loaded: {analyzer.get_num_units()} units")
    except Exception as e:
        logger.error(f"Failed to load analyzer: {e}"); sys.exit(1)

    logger.info("Extracting metrics...")
    try:
        q_metrics = analyzer.get_extension("quality_metrics").get_data()
        t_metrics = analyzer.get_extension("template_metrics").get_data()
        locations = analyzer.get_extension("unit_locations").get_data()
        q_metrics['loc_x'] = locations[:, 0]
        q_metrics['loc_y'] = locations[:, 1]
        q_metrics.to_excel(output_dir / "qm_unfiltered.xlsx")
        t_metrics.to_excel(output_dir / "tm_unfiltered.xlsx")
        logger.info("Saved unfiltered metrics")
    except Exception as e:
        logger.error(f"Failed to extract metrics: {e}"); traceback.print_exc(); sys.exit(1)

    plot_probe_locations(analyzer.recording, q_metrics.index.values,
                         locations, "locations_unfiltered.pdf", output_dir, logger)

    if args.no_curation:
        logger.info("Skipping curation — using all units")
        clean_units = q_metrics.index.values
    else:
        logger.info("Applying curation...")
        clean_metrics, rejection_log = apply_curation_logic(q_metrics, user_thresholds, logger)
        clean_units = clean_metrics.index.values
        clean_metrics.to_excel(output_dir / "metrics_curated.xlsx")
        rejection_log.to_excel(output_dir / "rejection_log.xlsx")
        t_metrics.loc[t_metrics.index.isin(clean_units)].to_excel(output_dir / "tm_curated.xlsx")
        logger.info(f"Curation: {len(clean_units)} / {len(q_metrics)} units passed")

    if len(clean_units) == 0:
        logger.warning("No units passed curation. Saving an explicit empty result.")
    else:
        mask = np.isin(analyzer.unit_ids, clean_units)
        plot_probe_locations(analyzer.recording, clean_units, locations[mask],
                             f"locations_{len(clean_units)}_units.pdf", output_dir, logger)
        plot_waveforms_grid(analyzer, clean_units, output_dir, logger)

    logger.info("Saving spike_times.npy for burst detection...")
    try:
        fs          = analyzer.recording.get_sampling_frequency()
        spike_times = {}
        missing     = []
        for uid in clean_units:
            try:
                spike_times[uid] = sorting.get_unit_spike_train(uid) / fs
            except KeyError:
                missing.append(uid)
        if missing:
            logger.warning(f"Skipping {len(missing)} units not found in sorting")
        np.save(output_dir / "spike_times.npy", spike_times)
        logger.info(f"Saved spike_times.npy with {len(spike_times)} units")
    except Exception as e:
        logger.error(f"Failed to save spike times: {e}"); traceback.print_exc(); sys.exit(1)

    summary = {
        "timestamp":         str(datetime.now()),
        "n_units_total":     int(len(q_metrics)),
        "n_units_curated":   int(len(clean_units)),
        "curation_applied":  not args.no_curation,
        "thresholds_used":   {**DEFAULT_THRESHOLDS, **(user_thresholds or {})},
        "status":            "no_detected_units" if len(q_metrics) == 0 else
                             "ok" if len(clean_units) else "no_curated_units",
        "spike_times_saved": str(output_dir / "spike_times.npy"),
    }
    with open(output_dir / "report_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    logger.info("=== REPORT GENERATION COMPLETE ===")
    logger.info(f"  Units curated: {len(clean_units)} / {len(q_metrics)}")
    logger.info(f"  Outputs in:    {output_dir}")


if __name__ == "__main__":
    main()
