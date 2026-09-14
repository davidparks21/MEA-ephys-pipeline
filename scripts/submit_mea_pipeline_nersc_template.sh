#!/bin/bash
#SBATCH --job-name=mea_ephys
#SBATCH --qos=regular
#SBATCH --constraint=gpu
#SBATCH --gpus=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=128GB
#SBATCH --time=03:00:00
#SBATCH --output=%x_%j.out
#SBATCH --error=%x_%j.err

set -euo pipefail

# SLURM copies submitted scripts to a spool directory.
# SLURM_SUBMIT_DIR preserves the directory where sbatch was called.
REPO_DIR="${SLURM_SUBMIT_DIR}"
source "${REPO_DIR}/run_config.env"

PIPELINE_DIR="${REPO_DIR}"
PARAMS_FILE="${REPO_DIR}/scripts/params_no_motion.json"

# Load the reproducible pipeline environment
module load conda/Miniforge3-25.9.1-0
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV"

NEXTFLOW_BIN="${REPO_DIR}/.tools/nextflow"

if [[ ! -x "$NEXTFLOW_BIN" ]]; then
    echo "ERROR: Validated Nextflow installation not found:"
    echo "  $NEXTFLOW_BIN"
    echo "Run: bash scripts/setup_nersc.sh"
    exit 1
fi

echo "Nextflow: $NEXTFLOW_BIN"
echo "Java:     $(command -v java)"
"$NEXTFLOW_BIN" -version
java -version

# Make required folders
mkdir -p "$RESULTS_PATH" "$WORK_DIR" "$LOG_DIR" "$TMPDIR" "$KACHERY_DIR"
mkdir -p "$RESULTS_PATH/nextflow"

# Runtime environment
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export KACHERY_DIR="$KACHERY_DIR"
export TMPDIR="$TMPDIR"
export PROJECT_DIR="$PROJECT_DIR"
export RESULTS_PATH="$RESULTS_PATH"

# Run pipeline
cd "$PIPELINE_DIR/pipeline"

"$NEXTFLOW_BIN" -C nextflow_nersc_template.config \
  -log "$RESULTS_PATH/nextflow/nextflow.log" \
  run main_multi_backend.nf \
  --input nwb \
  --ecephys_path "$DATA_DIR" \
  --runmode spikesort \
  --n_jobs 1 \
  --preprocessing_args "--motion skip --denoising cmr" \
  --spikesorting_args "--skip-motion-correction" \
  --params_file "$PARAMS_FILE" \
  --results_path "$RESULTS_PATH" \
  -work-dir "$WORK_DIR" \
  -resume
