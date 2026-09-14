#!/bin/bash
set -euo pipefail

echo "Loading NERSC Miniforge..."
module load conda/Miniforge3-25.9.1-0

source "$(conda info --base)/etc/profile.d/conda.sh"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NXF_VER="23.08.0-edge"
TOOLS_DIR="${REPO_DIR}/.tools"
NEXTFLOW_BIN="${TOOLS_DIR}/nextflow"

echo "Initializing Allen pipeline submodule..."
git -C "$REPO_DIR" submodule update --init --recursive

echo "Setting up env_ephys..."
if conda env list | awk '{print $1}' | grep -qx "env_ephys"; then
    conda env update \
        -n env_ephys \
        -f "$REPO_DIR/environment/nersc.yml" \
        --prune
else
    conda env create \
        -f "$REPO_DIR/environment/nersc.yml"
fi

conda activate env_ephys

echo "Installing validated Nextflow ${NXF_VER}..."
mkdir -p "$TOOLS_DIR"

NEED_NEXTFLOW=1

if [[ -x "$NEXTFLOW_BIN" ]]; then
    if "$NEXTFLOW_BIN" -version 2>&1 | grep -q "version ${NXF_VER}"; then
        NEED_NEXTFLOW=0
        echo "Validated Nextflow version already installed."
    fi
fi

if [[ "$NEED_NEXTFLOW" -eq 1 ]]; then
    rm -f "$NEXTFLOW_BIN"
    (
        cd "$TOOLS_DIR"
        curl -s https://get.nextflow.io | NXF_VER="$NXF_VER" bash
    )
    chmod +x "$NEXTFLOW_BIN"
fi

echo
echo "Installed versions:"
"$NEXTFLOW_BIN" -version
java -version

echo
echo "NERSC setup complete."
echo "Next:"
echo "  cp run_config.example.env run_config.env"
echo "  edit run_config.env"
echo "  scripts/run_nersc.sh"
