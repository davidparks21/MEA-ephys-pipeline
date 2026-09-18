import warnings

warnings.filterwarnings("ignore")

# GENERAL IMPORTS
import argparse
import sys
import os
import numpy as np
from pathlib import Path
import json
import time
import pandas as pd
import logging
from datetime import datetime, timedelta

# SPIKEINTERFACE
import spikeinterface as si
import spikeinterface.preprocessing as spre
import spikeinterface.widgets as sw

# needed to load extensions
import spikeinterface.postprocessing as spost
import spikeinterface.qualitymetrics as sqm

# VIZ
import matplotlib.pyplot as plt
import sortingview.views as vv

# AIND
from aind_data_schema.core.processing import DataProcess, ProcessStage
from aind_data_schema.components.identifiers import Code
from aind_data_schema_models.process_names import ProcessName

try:
    from aind_log_utils import log

    HAVE_AIND_LOG_UTILS = True
except ImportError:
    HAVE_AIND_LOG_UTILS = False

URL = "https://github.com/AllenNeuralDynamics/aind-ephys-visualization"
VERSION = "1.0"

GH_CURATION_REPO = os.getenv("GH_CURATION_REPO")
LABEL_CHOICES = ["noise", "MUA", "SUA", "pMUA", "pSUA"]

data_folder = Path("../data/")
scratch_folder = Path("../scratch")
results_folder = Path("../results/")

# Define argument parser
parser = argparse.ArgumentParser(description="Curate ecephys data")

output_format_group = parser.add_mutually_exclusive_group()
output_format_help = "Format for output figures. Default 'png'"
output_format_group.add_argument("static_output_format", nargs="?", default="png", help=output_format_help)
output_format_group.add_argument("--output-format", default="png", help=output_format_help)

n_jobs_group = parser.add_mutually_exclusive_group()
n_jobs_help = (
    "Number of jobs to use for parallel processing. Default is -1 (all available cores). "
    "It can also be a float between 0 and 1 to use a fraction of available cores"
)
n_jobs_group.add_argument("static_n_jobs", nargs="?", default="-1", help=n_jobs_help)
n_jobs_group.add_argument("--n-jobs", default="-1", help=n_jobs_help)

parser.add_argument("--params", default=None, help="Path to the parameters file or JSON string. If given, it will override all other arguments.")



if __name__ == "__main__":
    args = parser.parse_args()

    N_JOBS = args.static_n_jobs or args.n_jobs
    N_JOBS = int(N_JOBS) if not N_JOBS.startswith("0.") else float(N_JOBS)
    OUTPUT_FORMAT = args.static_output_format or args.output_format
    PARAMS = args.params

    if PARAMS is not None:
        try:
            # try to parse the JSON string first to avoid file name too long error
            visualization_params = json.loads(PARAMS)
        except json.JSONDecodeError:
            if Path(PARAMS).is_file():
                with open(PARAMS, "r") as f:
                    visualization_params = json.load(f)
            else:
                raise ValueError(f"Invalid parameters: {PARAMS} is not a valid JSON string or file path")
    else:
        with open("params.json", "r") as f:
            visualization_params = json.load(f)

    # Use CO_CPUS/N_JOBS_EXT env variable if available
    N_JOBS_EXT = os.getenv("CO_CPUS") or os.getenv("N_JOBS_EXT")
    N_JOBS = int(N_JOBS_EXT) if N_JOBS_EXT is not None else N_JOBS

    ecephys_sessions = [p for p in data_folder.iterdir() if "ecephys" in p.name.lower()]
    ecephys_session_folder = None
    if len(ecephys_sessions) == 1:
        ecephys_session_folder = ecephys_sessions[0]
        if HAVE_AIND_LOG_UTILS:
            # look for subject.json and data_description.json files
            subject_json = ecephys_session_folder / "subject.json"
            subject_id = "undefined"
            if subject_json.is_file():
                subject_data = json.load(open(subject_json, "r"))
                subject_id = subject_data["subject_id"]

            data_description_json = ecephys_session_folder / "data_description.json"
            session_name = "undefined"
            if data_description_json.is_file():
                data_description = json.load(open(data_description_json, "r"))
                session_name = data_description["name"]

            log.setup_logging(
                "Visualize Ecephys",
                subject_id=subject_id,
                asset_name=session_name,
            )
        else:
            logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    else:
        logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")

    data_process_prefix = "data_process_visualization"

    job_kwargs = visualization_params.pop("job_kwargs")
    job_kwargs["n_jobs"] = N_JOBS
    si.set_global_job_kwargs(**job_kwargs)

    ###### VISUALIZATION #########
    logging.info("\n\nVISUALIZATION")
    t_visualization_start_all = time.perf_counter()
    datetime_start_visualization = datetime.now()

    # check if test
    if (data_folder / "postprocessing_pipeline_output_test").is_dir():
        logging.info("\n*******************\n**** TEST MODE ****\n*******************\n")
        postprocessed_folder = data_folder / "postprocessing_pipeline_output_test"
        preprocessed_folder = data_folder / "preprocessing_pipeline_output_test"
        curation_folder = data_folder / "curation_pipeline_output_test"
        unit_classifier_folder = data_folder / "unit_classifier_pipeline_output_test"
        spikesorted_folder = data_folder / "spikesorting_pipeline_output_test"
    else:
        postprocessed_folder = data_folder
        preprocessed_folder = data_folder
        curation_folder = data_folder
        unit_classifier_folder = data_folder
        spikesorted_folder = data_folder
        data_processes_spikesorting_folder = data_folder

    # check kachery client
    kachery_set = os.environ.get("KACHERY_API_KEY", None)

    if kachery_set:
        logging.info(f"Kachery plots enabled")
        plot_kachery = True
    else:
        logging.info(f"Kachery plots disabled. KACHERY_API_KEY not found")
        plot_kachery = False

    # Retrieve recording_names from preprocessed folder
    recording_names = [
        "_".join(p.name.split("_")[1:])
        for p in preprocessed_folder.iterdir()
        if p.is_dir() and "preprocessed_" in p.name
    ]

    job_json_files = [p for p in data_folder.iterdir() if p.suffix == ".json" and "job" in p.name]
    job_dicts = []
    for job_json_file in job_json_files:
        with open(job_json_file) as f:
            job_dict = json.load(f)
        job_dicts.append(job_dict)
    logging.info(f"Found {len(job_dicts)} JSON job files")

    # loop through block-streams
    for recording_name in recording_names:
        t_visualization_start = time.perf_counter()
        datetime_start_visualization = datetime.now()
        visualization_output = {}

        recording_folder = preprocessed_folder / f"preprocessed_{recording_name}"
        analyzer_binary_folder = postprocessed_folder / f"postprocessed_{recording_name}"
        analyzer_zarr_folder = postprocessed_folder / f"postprocessed_{recording_name}.zarr"
        preprocessed_json_file = preprocessed_folder / f"preprocessedviz_{recording_name}.json"
        qc_file = curation_folder / f"qc_{recording_name}.npy"
        unit_classifier_file = unit_classifier_folder / f"unit_classifier_{recording_name}.csv"
        motion_folder = preprocessed_folder / f"motion_{recording_name}"
        visualization_output_process_json = results_folder / f"{data_process_prefix}_{recording_name}.json"
        # save vizualization output
        visualization_output_file = results_folder / f"visualization_{recording_name}.json"
        visualization_output_folder = results_folder / f"visualization_{recording_name}"
        visualization_output_folder.mkdir(exist_ok=True)

        logging.info(f"Visualizing recording: {recording_name}")

        with open(preprocessed_json_file, "r") as f:
            preprocessing_vizualization_data = json.load(f)

        recording_job_dict = None
        for job_dict in job_dicts:
            if recording_name in job_dict["recording_name"]:
                logging.info("\tFound JSON file associated to recording")
                recording_job_dict = job_dict
                break

        if recording_job_dict is not None:
            skip_times = recording_job_dict.get("skip_times", False)
            session_name = recording_job_dict["session_name"]
        else:
            skip_times = False

        # use spike locations
        skip_drift = False
        spike_locations_available = False
        # use spike locations
        analyzer_folder = None
        analyzer = None
        if analyzer_binary_folder.is_dir():
            analyzer_folder = analyzer_binary_folder
        elif analyzer_zarr_folder.is_dir():
            analyzer_folder = analyzer_zarr_folder
        motion_is_available = motion_folder.is_dir()

        if analyzer_folder is not None:
            try:
                # here recording_folder MUST exist
                assert recording_folder.is_dir(), f"Recording folder {recording_folder} does not exist"
                recording = si.load(recording_folder)
                analyzer = si.load(analyzer_folder, load_extensions=False)
                if skip_times:
                    recording.reset_times()
                if analyzer.has_extension("spike_locations"):
                    logging.info(f"\tVisualizing drift maps using spike sorted data")
                    spike_locations_available = True
            except Exception as e:
                logging.info(f"\tCould not load sorting analyzer or recording for {recording_name}.")

        # if spike locations are not available, detect and localize peaks
        if not spike_locations_available:
            if motion_is_available:
                drift_data = preprocessing_vizualization_data[recording_name]["drift"]
                recording = si.load(drift_data["recording"], base_folder=preprocessed_folder)
                if skip_times:
                    recording.reset_times()

                motion_info = spre.load_motion_info(motion_folder)
                logging.info(f"\tVisualizing drift maps using motion data (no spike sorting available)")
                peaks = motion_info["peaks"]
                peak_locations = motion_info["peak_locations"]
                peak_amps = peaks["amplitude"]
                if len(peaks) == 0:
                    logging.info("\t\tNo peaks detected. Skipping drift map")
                    skip_drift = True
            else:
                from spikeinterface.sortingcomponents.peak_detection import detect_peaks
                from spikeinterface.sortingcomponents.peak_localization import localize_peaks

                logging.info(f"\tVisualizing drift maps using detected peaks (no spike sorting available)")
                # locally_exclusive + pipeline steps LocalizeCenterOfMass + PeakToPeakFeature
                drift_data = preprocessing_vizualization_data[recording_name]["drift"]
                try:
                    recording = si.load(drift_data["recording"], base_folder=preprocessed_folder)
                    if skip_times:
                        recording.reset_times()

                    # SI 0.103 moved the internal node classes. Use the public
                    # APIs with the same detector and localization settings.
                    peaks = detect_peaks(
                        recording, method="locally_exclusive",
                        method_kwargs=visualization_params["drift"]["detection"],
                        job_kwargs=si.get_global_job_kwargs(),
                    )
                    logging.info(f"\t\tDetected {len(peaks)} peaks")
                    peak_amps = peaks["amplitude"]
                    if len(peaks) == 0:
                        logging.info("\t\tNo peaks detected. Skipping drift map")
                        skip_drift = True
                    else:
                        localization = visualization_params["drift"]["localization"]
                        peak_locations = localize_peaks(
                            recording, peaks, method="center_of_mass",
                            method_kwargs={"radius_um": localization["radius_um"]},
                            ms_before=localization["ms_before"],
                            ms_after=localization["ms_after"],
                            job_kwargs=si.get_global_job_kwargs(),
                        )
                except Exception as e:
                    logging.warning(f"\t\tCould not generate drift map. Skipping: {e}", exc_info=True)
                    skip_drift = True

        if not skip_drift:
            fig_drift, axs_drift = plt.subplots(
                ncols=recording.get_num_segments(), figsize=visualization_params["drift"]["figsize"]
            )
            y_locs = recording.get_channel_locations()[:, 1]
            depth_lim = [np.min(y_locs), np.max(y_locs)]

            for segment_index in range(recording.get_num_segments()):
                if recording.get_num_segments() == 1:
                    ax_drift = axs_drift
                else:
                    ax_drift = axs_drift[segment_index]
                if spike_locations_available:
                    sorting_analyzer_to_plot = analyzer
                    peaks_to_plot = None
                    peak_locations_to_plot = None
                    sampling_frequency = None
                else:
                    sorting_analyzer_to_plot = None
                    peaks_to_plot = peaks
                    peak_locations_to_plot = peak_locations
                    sampling_frequency = recording.sampling_frequency

                _ = sw.plot_drift_raster_map(
                    sorting_analyzer=sorting_analyzer_to_plot,
                    peaks=peaks_to_plot,
                    peak_locations=peak_locations_to_plot,
                    sampling_frequency=sampling_frequency,
                    segment_index=segment_index,
                    depth_lim=depth_lim,
                    clim=(visualization_params["drift"]["vmin"], visualization_params["drift"]["vmax"]),
                    cmap=visualization_params["drift"]["cmap"],
                    scatter_decimate=visualization_params["drift"]["n_skip"],
                    alpha=visualization_params["drift"]["alpha"],
                    ax=ax_drift,
                )
                ax_drift.spines["top"].set_visible(False)
                ax_drift.spines["right"].set_visible(False)

            drift_map_fig_file = visualization_output_folder / f"drift_map.{OUTPUT_FORMAT}"
            fig_drift.savefig(drift_map_fig_file, dpi=300)

            # make a sorting view View
            v_drift = None
            if plot_kachery:
                if OUTPUT_FORMAT != "png":
                    # kachery needs a png
                    drift_map_fig_file = scratch_folder / f"{recording_name}_map.png"
                    fig_drift.savefig(drift_map_fig_file, dpi=300)

                v_drift = vv.TabLayoutItem(
                    label=f"Drift map", view=vv.Image(image_path=str(drift_map_fig_file))
                )

            # plot motion
            v_motion = None
            if motion_is_available:
                motion_info = spre.load_motion_info(motion_folder)

                if motion_info["motion"] is not None:
                    logging.info("\tVisualizing motion")

                    cmap = visualization_params["motion"]["cmap"]
                    scatter_decimate = visualization_params["motion"]["scatter_decimate"]
                    figsize = visualization_params["motion"]["figsize"]

                    fig_motion = plt.figure(figsize=figsize)
                    # motion correction is performed after concatenation
                    # since multi-segment is not supported
                    if recording.get_num_segments() > 1:
                        recording_c = si.concatenate_recordings([recording])
                    else:
                        recording_c = recording

                    w_motion = sw.plot_motion_info(
                        motion_info,
                        recording=recording_c,
                        figure=fig_motion,
                        color_amplitude=True,
                        amplitude_cmap=cmap,
                        scatter_decimate=scatter_decimate,
                    )
                    motion_fig_file = visualization_output_folder / f"motion.{OUTPUT_FORMAT}"
                    fig_motion.savefig(motion_fig_file, dpi=300)

                    # make a sorting view View
                    if plot_kachery:
                        if OUTPUT_FORMAT != "png":
                            # kachery needs a png
                            motion_fig_file = scratch_folder / f"{recording_name}_motion.png"
                            fig_motion.savefig(motion_fig_file, dpi=300)

                        v_motion = vv.TabLayoutItem(
                            label=f"Motion",
                            view=vv.Image(image_path=str(motion_fig_file)),
                        )

        # timeseries
        logging.info(f"\tVisualizing traces")
        timeseries_tab_items = []

        timeseries_data = preprocessing_vizualization_data[recording_name]["timeseries"]
        recording_full_dict = timeseries_data["full"]
        recording_proc_dict = timeseries_data["proc"]

        # get random chunks to estimate clims
        clims_full = {}
        recording_full_loaded = {}
        for layer, rec_dict in recording_full_dict.items():
            try:
                rec = si.load(rec_dict, base_folder=data_folder)
                if skip_times:
                    rec.reset_times()
            except Exception as e:
                logging.info(f"\t\tCould not load full layer {layer}.\nError:\n{e}")
                continue
            chunk = si.get_random_data_chunks(rec)
            max_value = np.quantile(chunk, 0.99) * 1.2
            clims_full[layer] = (-max_value, max_value)
            # explicitly set timestamps if not present
            for segment_index in range(rec.get_num_segments()):
                if not rec.has_time_vector(segment_index=segment_index):
                    times = rec.get_times(segment_index=segment_index)
                    rec.set_times(times, segment_index=segment_index, with_warning=False)
            recording_full_loaded[layer] = rec
        clims_proc = {}
        if recording_proc_dict is not None:
            recording_proc_loaded = {}
            for layer, rec_dict in recording_proc_dict.items():
                try:
                    rec = si.load(rec_dict, base_folder=data_folder)
                    if skip_times:
                        rec.reset_times()
                except Exception as e:
                    logging.info(f"\t\tCould not load processed layer {layer}.\nError:\n{e}")
                    continue
                chunk = si.get_random_data_chunks(rec)
                max_value = np.quantile(chunk, 0.99) * 1.2
                clims_proc[layer] = (-max_value, max_value)
                # explicitly set timestamps if not present
                for segment_index in range(rec.get_num_segments()):
                    if not rec.has_time_vector(segment_index=segment_index):
                        times = rec.get_times(segment_index=segment_index)
                        rec.set_times(times, segment_index=segment_index, with_warning=False)
                recording_proc_loaded[layer] = rec
        else:
            logging.info(f"\tPreprocessed timeseries not avaliable")

        fs = recording.sampling_frequency
        n_snippets_per_seg = visualization_params["timeseries"]["n_snippets_per_segment"]

        max_full_layers = len(recording_full_dict)
        max_proc_layers = len(recording_proc_dict) if recording_proc_dict is not None else 0
        max_num_layers = max(max_full_layers, max_proc_layers)

        for segment_index in range(recording.get_num_segments()):
            traces_figsize = (int(5 * max_num_layers), int(5 * n_snippets_per_seg))
            fig_ts, axs_ts = plt.subplots(
                ncols=n_snippets_per_seg,
                nrows=max_num_layers,
                figsize=traces_figsize,
            )
            fig_ts.suptitle("Full traces", fontsize=16)
            fig_ts_proc = None
            if recording_proc_dict is not None:
                fig_ts_proc, axs_ts_proc = plt.subplots(
                    ncols=n_snippets_per_seg,
                    nrows=max_num_layers,
                    figsize=traces_figsize,
                )
                fig_ts_proc.suptitle("Processed traces", fontsize=16)

            # evenly distribute t_starts across segments
            times = recording.get_times(segment_index=segment_index)
            t_starts = np.linspace(times[0], times[-1], n_snippets_per_seg + 2)[1:-1]

            for i_t, t_start in enumerate(t_starts):
                time_range = np.round(
                    np.array([t_start, t_start + visualization_params["timeseries"]["snippet_duration_s"]]), 1
                )
                if plot_kachery:
                    try:
                        w_full = sw.plot_traces(
                            recording_full_loaded,
                            order_channel_by_depth=True,
                            time_range=time_range,
                            segment_index=segment_index,
                            clim=clims_full,
                            mode="map",
                            backend="sortingview",
                            generate_url=False,
                        )
                        if recording_proc_dict is not None:
                            w_proc = sw.plot_traces(
                                recording_proc_loaded,
                                order_channel_by_depth=True,
                                time_range=time_range,
                                segment_index=segment_index,
                                clim=clims_proc,
                                mode="map",
                                backend="sortingview",
                                generate_url=False,
                            )
                            view = vv.Splitter(
                                direction="horizontal",
                                item1=vv.LayoutItem(w_full.view),
                                item2=vv.LayoutItem(w_proc.view),
                            )
                        else:
                            view = w_full.view
                        v_item = vv.TabLayoutItem(
                            label=f"Timeseries - Segment {segment_index} - Time: {time_range}", view=view
                        )
                        timeseries_tab_items.append(v_item)
                    except Exception as e:
                        logging.info(
                            f"\t\tError plotting traces with SortingView for "
                            f"{recording_name} - {segment_index} - {time_range}."
                        )

                for i_l, (layer, rec) in enumerate(recording_full_loaded.items()):
                    ax_ts = axs_ts[i_t] if max_num_layers == 1 else axs_ts[i_l, i_t]
                    sw.plot_traces(
                        rec,
                        order_channel_by_depth=True,
                        time_range=time_range,
                        segment_index=segment_index,
                        ax=ax_ts,
                        clim=clims_full[layer],
                        backend="matplotlib",
                    )
                    if i_l == 0:
                        ax_ts.set_title(f"Time: {time_range}\n{layer}")
                    else:
                        ax_ts.set_title(f"{layer}")
                    ax_ts.spines["top"].set_visible(False)
                    ax_ts.spines["right"].set_visible(False)
                if recording_proc_dict is not None:
                    for i_l, (layer, rec) in enumerate(recording_proc_loaded.items()):
                        ax_ts_proc = axs_ts_proc[i_t] if max_num_layers == 1 else axs_ts_proc[i_l, i_t]
                        sw.plot_traces(
                            rec,
                            order_channel_by_depth=True,
                            time_range=time_range,
                            segment_index=segment_index,
                            ax=ax_ts_proc,
                            clim=clims_proc[layer],
                            backend="matplotlib",
                        )
                        if i_l == 0:
                            ax_ts_proc.set_title(f"Time: {time_range}\n{layer}")
                        else:
                            ax_ts_proc.set_title(f"{layer}")
                        ax_ts_proc.spines["top"].set_visible(False)
                        ax_ts_proc.spines["right"].set_visible(False)

            fig_ts.savefig(visualization_output_folder / f"traces_full_seg{segment_index}.{OUTPUT_FORMAT}", dpi=300)
            if fig_ts_proc is not None:
                fig_ts_proc.savefig(visualization_output_folder / f"traces_proc_seg{segment_index}.{OUTPUT_FORMAT}", dpi=300)

        if plot_kachery:
            if not skip_drift:
                # add drift map if available
                if v_drift is not None:
                    timeseries_tab_items.append(v_drift)

                # add motion if available
                if v_motion is not None:
                    timeseries_tab_items.append(v_motion)

            v_timeseries = vv.TabLayout(items=timeseries_tab_items)
            try:
                url = v_timeseries.url(label=f"{session_name} - {recording_name}")
                logging.info(f"\n{url}\n")
                visualization_output["timeseries"] = url
            except Exception as e:
                logging.info(f"Figurl-Sortingview plotting error.")

        # sorting summary
        skip_sorting_summary = True
        if analyzer is not None:
            logging.info(f"\tVisualizing sorting summary")
            skip_sorting_summary = False

        if not skip_sorting_summary:
            displayed_unit_properties = []
            extra_unit_properties = {}
            # add firing rate, snr, and amplitude columns
            if analyzer.has_extension("quality_metrics"):
                qm = analyzer.get_extension("quality_metrics").get_data()
                if "firing_rate" in qm.columns:
                    displayed_unit_properties.append("firing_rate")
                if "snr" in qm.columns:
                    displayed_unit_properties.append("snr")

            amplitudes = si.get_template_extremum_amplitude(analyzer, mode="peak_to_peak")
            extra_unit_properties["amplitude"] = np.array(list(amplitudes.values()))

            # add curation column
            if qc_file.is_file():
                # add qc property to analyzer sorting
                default_qc = np.load(qc_file)
                extra_unit_properties["default_qc"] = default_qc

            # add noise decoder column
            if unit_classifier_file.is_file():
                # add decoder_label and decoder probability
                unit_classifier_df = pd.read_csv(unit_classifier_file, index_col=False)
                if len(unit_classifier_df) == len(analyzer.unit_ids):
                    decoder_label = unit_classifier_df["decoder_label"]
                    extra_unit_properties["decoder_label"] = decoder_label.values.astype(str)
                    decoder_prob = unit_classifier_df["decoder_probability"].to_numpy(dtype=float)
                    extra_unit_properties["decoder_prob"] = np.round(decoder_prob, 2)
                else:
                    logging.info(f"\t\tCould not load unit classification data for {recording_name}")

            # retrieve sorter name (if spike sorting was performed)
            data_process_spikesorting_json = spikesorted_folder / f"data_process_spikesorting_{recording_name}.json"
            if data_process_spikesorting_json.is_file():
                with open(data_process_spikesorting_json, "r") as f:
                    data_process_spikesorting = json.load(f)
                    code_metadata = data_process_spikesorting.get("code")
                    if code_metadata:
                        sorter_name = code_metadata["parameters"]["sorter_name"]
                    elif "parameters" in data_process_spikesorting:
                        sorter_name = data_process_spikesorting["parameters"]["sorter_name"]
                    else:
                        sorter_name = "unknown"
            else:
                sorter_name = "unknown"

            if len(analyzer.unit_ids) > 0:
                if plot_kachery:
                    # tab layout with Summary and Quality Metrics
                    v_qm = sw.plot_quality_metrics(
                        analyzer,
                        skip_metrics=["isi_violations_count", "rp_violations"],
                        include_metrics_data=True,
                        backend="sortingview",
                        generate_url=False,
                    ).view
                    v_sorting = sw.plot_sorting_summary(
                        analyzer,
                        displayed_unit_properties=displayed_unit_properties,
                        extra_unit_properties=extra_unit_properties,
                        curation=True,
                        label_choices=LABEL_CHOICES,
                        backend="sortingview",
                        generate_url=False,
                    ).view

                    v_summary = vv.TabLayout(
                        items=[
                            vv.TabLayoutItem(label="Sorting summary", view=v_sorting),
                            vv.TabLayoutItem(label="Quality Metrics", view=v_qm),
                        ]
                    )

                    try:
                        # pre-generate gh for curation
                        if GH_CURATION_REPO is not None:
                            gh_path = f"{GH_CURATION_REPO}/{session_name}/{recording_name}/{sorter_name}/curation.json"
                            state = dict(sortingCuration=gh_path)
                        else:
                            state = None
                        url = v_summary.url(
                            label=f"{session_name} - {recording_name} - {sorter_name} - Sorting Summary", state=state
                        )
                        logging.info(f"\n{url}\n")
                        visualization_output["sorting_summary"] = url

                    except Exception as e:
                        logging.info("\tSortingview plotting resulted in an error")
                else:
                    logging.info(f"\tSkipping sorting summary visualization for {recording_name}. Kachery client not found.")
            else:
                logging.info(f"\tSkipping sorting summary visualization for {recording_name}. No units after curation.")
        else:
            logging.info(f"\tSkipping sorting summary visualization for {recording_name}. No sorting information available.")

        # save params in output
        visualization_notes = json.dumps(visualization_output, indent=4)
        # replace special characters
        visualization_notes = visualization_notes.replace('\\"', "%22")
        visualization_notes = visualization_notes.replace("#", "%23")

        if plot_kachery:
            # remove escape characters
            visualization_output_file.write_text(visualization_notes)

        # save vizualization output
        t_visualization_end = time.perf_counter()
        elapsed_time_visualization = np.round(t_visualization_end - t_visualization_start, 2)

        visualization_params["recording_name"] = recording_name

        visualization_process = DataProcess(
            process_type=ProcessName.EPHYS_VISUALIZATION,
            stage=ProcessStage.PROCESSING,
            name="Ephys visualization",
            experimenters=["Alessio Buccino"],
            code=Code(
                url=URL,
                version=VERSION, # either release or git commit
                parameters=visualization_params
            ),
            start_date_time=datetime_start_visualization,
            end_date_time=datetime_start_visualization + timedelta(seconds=np.floor(elapsed_time_visualization)),
            output_path=str(results_folder),
            output_parameters=visualization_output,
            notes=visualization_notes,
        )
        with open(visualization_output_process_json, "w") as f:
            f.write(visualization_process.model_dump_json(indent=3))

    # save vizualization output
    t_visualization_end_all = time.perf_counter()
    elapsed_time_visualization_all = np.round(t_visualization_end_all - t_visualization_start_all, 2)

    logging.info(f"VISUALIZATION time: {elapsed_time_visualization_all}s")
