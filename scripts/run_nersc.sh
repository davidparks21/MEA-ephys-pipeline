#!/bin/bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CONFIG="${REPO_DIR}/run_config.env"

if [[ ! -f "$CONFIG" ]]; then
    echo "ERROR: run_config.env not found."
    echo
    echo "Create it with:"
    echo "  cp run_config.example.env run_config.env"
    echo
    echo "Then edit PROJECT_DIR and NERSC_ACCOUNT."
    exit 1
fi

source "$CONFIG"

if [[ -z "${NERSC_ACCOUNT:-}" || "$NERSC_ACCOUNT" == "<YOUR_NERSC_ACCOUNT>" ]]; then
    echo "ERROR: Set NERSC_ACCOUNT in run_config.env"
    exit 1
fi

mkdir -p \
    "$DATA_DIR" \
    "$RESULTS_PATH" \
    "$WORK_DIR" \
    "$LOG_DIR" \
    "$TMPDIR" \
    "$KACHERY_DIR"

cd "$REPO_DIR"

echo "Submitting MEA pipeline"
echo "Account: $NERSC_ACCOUNT"
echo "Project: $PROJECT_DIR"
echo "Input:   $DATA_DIR"
echo "Results: $RESULTS_PATH"

sbatch \
    --account="$NERSC_ACCOUNT" \
    scripts/submit_mea_pipeline_nersc_template.sh
