# Running on NRP

The Nextflow driver and processing tasks run as Kubernetes Jobs sharing a
ReadWriteMany PVC. `pipeline/nextflow_nrp.config` provides CPU and GPU profiles;
`scripts/nrp/` contains submission and synthetic-test helpers.

## Requirements

- Python 3 and `kubectl` on the control machine, with access to context `nautilus`.
- A namespace with `nextflow-sa` permitted to manage task Jobs and read their logs.
  The submission helper defaults to `braingeneers`; use `--namespace` to override it.
- The `rook-cephfs` storage class and capacity for a dedicated 20 GiB PVC.
- Access from task containers to the pinned container images and capsule Git repositories.

The driver uses Nextflow 26.04.6 with `nf-k8s` 1.5.4. The helper selects
`NXF_SYNTAX_PARSER=v1` for the existing Groovy DSL2 workflow and preserves the
runner image's installed framework/plugin paths. Image digests are recorded in
`scripts/nrp/images.json` and the NRP configuration.

## Run the synthetic smoke test

Run commands from the repository root. Choose a unique lowercase test ID of at
most 35 characters. The fixture contains 180 seconds of data, 32 channels and
20 ground-truth units at 30 kHz, with equivalent signed and unsigned NWBs.
The workflow helper uses the signed fixture and disables motion correction.

```bash
TEST_ID=mea-smoke-example
python3 scripts/nrp/submit.py --test-id "$TEST_ID" init
python3 scripts/nrp/submit.py --test-id "$TEST_ID" job prepare \
  --image nwb --cpus 2 --memory 8Gi \
  --command 'python -u {source}/scripts/nrp/prepare_fixture.py --output {root}/data/fixture'

python3 scripts/nrp/submit.py --test-id "$TEST_ID" status
kubectl --context nautilus -n braingeneers logs "job/$TEST_ID-prepare" -c main
```

After preparation succeeds, launch the two workflows. They use separate work,
cache and results directories; only GPU Kilosort4 tasks request a GPU.

```bash
for device in cpu gpu; do
  python3 scripts/nrp/submit.py --test-id "$TEST_ID" job "workflow-$device" \
    --image driver --driver --deadline 7200 \
    --command "python3 -u {source}/scripts/nrp/run_workflow.py --device $device"
done
```

Check `status` and Job logs before submitting dependent work. Resource defaults
are sized for this small test; adjust CPUs, memory, time and PVC capacity for
larger recordings. The smoke test does not establish biological validity or
production resource requirements.

## Validate and resume

After both drivers finish, validate analyzers and reports, then NWB exports:

```bash
python3 scripts/nrp/submit.py --test-id "$TEST_ID" job validate-analyzers \
  --image base --cpus 2 --memory 4Gi \
  --command 'python -u {source}/scripts/nrp/validate.py analyzers'

# Submit after analyzer validation succeeds.
python3 scripts/nrp/submit.py --test-id "$TEST_ID" job validate-nwb \
  --image nwb --cpus 2 --memory 4Gi \
  --command 'python -u {source}/scripts/nrp/validate_nwb.py'
```

Analyzer validation uses the producing base image because its NumPy version
differs from the NWB image. `validate_nwb.py` installs the isolated, pinned
validation dependencies in `scripts/nrp/nwb_validation_requirements.txt` without
changing the processing environment. These validators expect the synthetic fixture.

To resume, submit a new driver Job with `--resume`:

```bash
python3 scripts/nrp/submit.py --test-id "$TEST_ID" job resume-cpu \
  --image driver --driver --deadline 7200 \
  --command 'python3 -u {source}/scripts/nrp/run_workflow.py --device cpu --resume'
```

Use `--device gpu` for the GPU run. Keep both the launch cache and work directories,
and run at most one driver per device at a time. For a cache-only check after
helper edits, add `--source-snapshot <id>` using the successful run's source ID.
Omit it when testing changed workflow code. Publication overwrites matching
outputs on resume; wait for all file transfers to finish before validating.

## Inputs and outputs

`run_workflow.py` launches the fixture test. For other recordings, invoke
`pipeline/main_multi_backend.nf` with the NRP configuration and these parameters:

| Parameter | Meaning |
| --- | --- |
| `--ecephys_path` | Directory containing a single NWB recording; falls back to `DATA_PATH`, then `DATA_DIR`. |
| `--results_path` | Publication directory; falls back to `RESULTS_PATH`, then `<launchDir>/results`. |
| `--params_file` | Nested stage JSON, such as `scripts/params_no_motion.json`; distinct from Nextflow's `-params-file`. |
| `--torch_device` | `cpu` or `cuda` for Kilosort4; selected by the CPU/GPU profiles. |

All tasks must see the input and serialized recording dependencies, normally on
the shared PVC. Capsule JSON can replace defaults, so provide complete settings.
Forward arguments beginning with dashes using equals syntax, for example
`--preprocessing_args='--motion skip'`.

Fixture outputs are under `/workspace/<test-id>/runs/<device>/results/`, including
`reports/<recording>/`, `bursts/<recording>/` and `nwb/`. An NWB export may be a Zarr
directory with an `.nwb` suffix. Retain input recordings for analyzer references.
Nextflow logs, traces and reports are under the corresponding `evidence/` directory.

## Collect artifacts

```bash
python3 scripts/nrp/submit.py --test-id "$TEST_ID" collect-logs
python3 scripts/nrp/submit.py --test-id "$TEST_ID" job archive \
  --image driver --cpus 2 --memory 2Gi --deadline 7200 \
  --command 'python3 -u {source}/scripts/nrp/archive.py'
```

The archive and checksum manifest are written to
`/workspace/<test-id>/artifacts.tar.gz` and `archive.json`. Copy them to durable
storage and verify the checksum before deleting the dedicated PVC. Archives
preserve symlinks and serialized recording paths; restore the original root or
remap paths to reuse analyzers and caches. Kubernetes evidence is collected under
ignored `logs/nrp/<test-id>/`. Test resources carry the label `mea-test=<test-id>`;
use that label to scope cleanup after all Jobs finish and artifacts are retained.
