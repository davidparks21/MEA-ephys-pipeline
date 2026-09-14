# MEA Ephys Pipeline on NERSC

Reusable NERSC/Perlmutter setup for the MEA electrophysiology Nextflow pipeline.

The NERSC setup has been validated with:

- Nextflow 23.08.0-edge
- Java 17
- Python 3.11
- NWB input
- Kilosort4 GPU spike sorting
- preprocessing
- postprocessing
- curation
- visualization
- report generation
- burst detection
- NWB export

## Quick start

### 1. Clone the NERSC branch

```bash
git clone -b nersc-reusable-setup \
  https://github.com/BenShalomLab/MEA-ephys-pipeline.git

cd MEA-ephys-pipeline
```

### 2. Set up the NERSC environment

```bash
bash scripts/setup_nersc.sh
```

This automatically:

- initializes the Allen Neural Dynamics Git submodule
- creates or updates the `env_ephys` Conda environment
- installs Nextflow 23.08.0-edge
- installs Java 17
- verifies the installed versions

Users do not need to install Java or Nextflow manually or modify `.bashrc`. The setup script installs Java through Conda and pins the validated Nextflow 23.08.0-edge executable inside the repository.

### 3. Create the run configuration

```bash
cp run_config.example.env run_config.env
nano run_config.env
```

Normally only two values need to be changed:

```bash
export NERSC_ACCOUNT=<YOUR_NERSC_ACCOUNT>
export PROJECT_DIR=/pscratch/sd/<first-letter>/<username>/mea_pipeline_run
```

### 4. Add input data

Put NWB input files in:

```text
$PROJECT_DIR/data
```

### 5. Submit the pipeline

From the repository root:

```bash
scripts/run_nersc.sh
```

The launcher reads `run_config.env`, creates the required directories, and submits the SLURM job with the selected NERSC account.

## Monitor the run

```bash
squeue -u $USER
```

To follow Nextflow:

```bash
source run_config.env
tail -f "$RESULTS_PATH/nextflow/nextflow.log"
```

The pipeline uses `-resume`, so completed processes can be reused when the same work directory is retained.

## Pipeline stages

1. job dispatch
2. preprocessing
3. Kilosort4 spike sorting
4. postprocessing
5. curation
6. visualization
7. report generation
8. burst detection
9. results collection
10. NWB export

## Configuration files

Main reusable configuration files:

```text
run_config.example.env
environment/nersc.yml
pipeline/nextflow_nersc_template.config
pipeline/capsule_versions.env
scripts/params_no_motion.json
```

`pipeline/capsule_versions.env` is the source of truth for tested capsule versions.

The validated SpikeInterface version is:

```text
0.103.2
```

Do not independently hardcode a different SpikeInterface version in the NERSC setup.

## Main outputs

Results are written under:

```text
$PROJECT_DIR/results
```

Important output directories include:

```text
results/nwb
results/spikesorted
results/curated
results/postprocessed
results/visualization
results/nextflow
```

Nextflow summary files include:

```text
results/nextflow/dag.html
results/nextflow/report.html
results/nextflow/timeline.html
results/nextflow/trace.txt
```

## Custom analysis outputs

Report generation may produce:

```text
waveforms_grid.pdf
locations_unfiltered.pdf
locations_206_units.pdf
metrics_curated.xlsx
qm_unfiltered.xlsx
rejection_log.xlsx
report_summary.json
spike_times.npy
```

Burst detection may produce:

```text
network_results.json
raster_burst_plot.png
raster_burst_plot.svg
raster_burst_plot_30s.png
raster_burst_plot_30s.svg
raster_burst_plot_60s.svg
burst_detection.log
```

## Curation

The default parameter file is:

```text
scripts/params_no_motion.json
```

The current default curation rule is:

```text
isi_violations_ratio < 0.5 and presence_ratio > 0.8 and firing_rate > 0.1
```

Users can modify the curation criteria for their experiment.

## Troubleshooting notes

Java is installed inside the `env_ephys` Conda environment. The validated Nextflow 23.08.0-edge executable is installed separately under `.tools/nextflow` by `scripts/setup_nersc.sh`.

Do not add:

```text
module load openjdk/17
```

That module was not available on Perlmutter during testing.

Do not add guessed HTTP or HTTPS proxy settings unless required by current NERSC documentation.

If a container runtime fails, troubleshoot the runtime separately before changing Python, SpikeInterface, or capsule versions.
