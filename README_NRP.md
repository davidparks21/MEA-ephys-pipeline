# Running on NRP

The Nextflow driver and processing tasks run as Kubernetes Jobs sharing a
ReadWriteMany PVC. `pipeline/nextflow_nrp.config` provides CPU and GPU profiles;
`scripts/nrp/` contains submission and synthetic-test helpers.

## Separation from NERSC

NRP is selected explicitly with `-C pipeline/nextflow_nrp.config`. Its Kubernetes
settings, GPU allocation, image digests, thread limits, and runtime/plugin setup
are confined to that configuration and `scripts/nrp/`. The existing NERSC
configuration, launchers, Conda environment and Nextflow 23.08.0-edge pin are
unchanged. NERSC does not need Kubernetes, the NRP driver, or NRP environment
variables.

NRP sets the capsule worker and BLAS/OpenMP/Numba limits from each task's allocated
CPUs using a `beforeScript` hook. Other backends retain their existing environment
and the original Slurm-specific worker handling. NRP also enables
`params.publish_overwrite = true` so resumes refresh published copies. Without
that override, the shared workflow preserves Nextflow's defaults: overwrite on a
normal run, but keep existing published files on a resumed run.

The shared workflow still includes correctness fixes that also affect NERSC:

| Shared fix | Behavior to review on NERSC |
| --- | --- |
| Preprocessing and LFP dtype guards | Convert unsigned recordings only; signed recordings no longer fail the conversion. |
| Input/output and parameter handling | Honor explicit paths consistently, retain environment fallbacks, and reject malformed stage JSON. |
| Corrected no-motion JSON | Pass the nested settings to Kilosort; settings previously ignored now take effect and can change sorting results. |
| Vendored pinned capsules | Run this checkout's preprocessing/NWB code with the dtype guards; licenses and original pins are retained. |
| Report/burst staging and inputs | Use this checkout's code and one explicit analyzer/spike dictionary per recording; publish under `reports/<recording>/` and `bursts/<recording>/`. |
| Empty report/burst results | Save explicit empty outputs when no units survive, preserving the curation thresholds. |
| Optional device/publication controls | Device selection stays automatic unless requested; publication uses the backend's optional overwrite setting. |

These fixes remain shared to keep one processing implementation. Separating them
would duplicate workflow/capsule logic or preserve known failures on one backend.
The NRP config alone has not been validated as a drop-in for an unmodified partner
checkout; share this branch together with the config and this guide.

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
| `--publish_overwrite` | Optional `true`/`false`; unset preserves Nextflow's defaults. NRP config sets `true`. |

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

## Compatibility checks and NERSC handoff

After fixture preparation, the focused checks can run as finite NRP Jobs:

```bash
python3 scripts/nrp/submit.py --test-id "$TEST_ID" job compat-nrp \
  --image driver --driver --deadline 2400 \
  --command 'python3 -u {source}/scripts/nrp/check_compatibility.py nrp'
python3 scripts/nrp/submit.py --test-id "$TEST_ID" job compat-nersc \
  --image driver --deadline 2400 \
  --command 'python3 -u {source}/scripts/nrp/check_compatibility.py nersc'
```

The first checks the real NRP task environment at 1, 2 and 4 CPUs. Both check
publication and cached resumes with default, enabled and disabled overwriting.
The second downloads checksum-pinned Java 17 and Nextflow 23.08.0-edge into an
isolated ephemeral test cache, previews the shared workflow with the NERSC launch arguments,
and checks environment fallbacks and invalid JSON. Its tiny shell-only execution
tests explicitly disable Shifter. Results are under `validation/compatibility-*.json`;
commands, logs and task files are under `probes/compatibility/`.

**A real NERSC/Shifter smoke test is still required.** The NRP checks do not
validate NERSC GPU exposure, Shifter mounts or filesystem behavior. For that test:

1. Use a separate checkout of this branch on NERSC. Follow `README_NERSC.md` and
   run the existing setup script; retain its Nextflow 23.08.0-edge installation.
2. Create that checkout's ignored `run_config.env` from the existing example.
   Set the usual account and a **fresh** `PROJECT_DIR`, keeping previous runs intact.
   Put one supported NWB recording in `DATA_DIR`; retain the original input.
3. Run `bash scripts/run_nersc.sh` unchanged. Check that all stages complete,
   Kilosort uses the allocated GPU, and effective worker settings fit the allocation.
   Confirm analyzer, report/burst and NWB outputs are readable. Empty curated
   results are valid if the input does not pass the existing thresholds.
4. Run the same launcher again after completion and check cached task reuse.
   Existing published files should remain unchanged under NERSC's default resume
   policy; use a fresh results directory when comparing new processing changes.
5. Retain the Slurm stdout/stderr, Nextflow log/trace, task `.command.*` files,
   runtime versions and effective sorter settings. Compare with the established
   lab run, accounting for the shared fixes listed above.

Do not run two drivers in the same launch/work directory concurrently. NRP and
older-runtime checks must be reported separately from this pending NERSC test.
