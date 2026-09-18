# Running on NRP

The Nextflow driver and processing tasks run as Kubernetes Jobs sharing a
ReadWriteMany PVC. `pipeline/nextflow_nrp.config` provides CPU and GPU profiles;
`scripts/nrp/` contains submission, input-inspection and synthetic-test helpers.

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
| Resume cache keys | Include stage argument values and capsule revisions when deciding whether work can be reused; changed settings previously could reuse stale tasks with the legacy parser. |
| Corrected no-motion JSON | Pass the nested settings to Kilosort; settings previously ignored now take effect and can change sorting results. |
| Vendored pinned capsules | Run this checkout's dispatch, preprocessing, KS4, postprocessing, visualization and both NWB capsules; licenses and original pins are retained. |
| Generic input dispatch | Vendored dispatch recognizes Neo reader classes, selects streams/blocks, and preserves supplied probe names through serialization for multiwell NWB export. Its NWB loader is unchanged. |
| LFP export | Retain the fractional final second; materialize contiguous channels before spatial selection to bound HDF5 indexing costs. Temporary LFP binaries contain all channels; exported channel selection is unchanged. |
| Report/burst staging and inputs | Use this checkout's code and one explicit analyzer/spike dictionary per recording; publish under `reports/<recording>/` and `bursts/<recording>/`. |
| Empty report/burst results | Save explicit empty outputs when no units survive, preserving the curation thresholds. |
| Zero-spike recordings | Preserve Kilosort's confirmed zero-detection result as an empty sorting/analyzer with status and original logs; unrelated sorter errors still fail. Metrics requiring spikes are explicitly inapplicable. |
| Visualization fallback | Use the pinned SpikeInterface version's public peak detection/localization APIs when sorted-spike locations and motion data are absent; handle empty classifier tables and preserve detector settings and trace plots. |
| Optional device/publication controls | Device selection stays automatic unless requested; publication uses the backend's optional overwrite setting. |

These fixes remain shared to keep one processing implementation. Separating them
would duplicate workflow/capsule logic or preserve known failures on one backend.
Vendored capsules use the code in this checkout; changing their original pin in
`capsule_versions.env` alone does not update that code. The repository-prefix
override applies to capsules that are still fetched at runtime.
The NRP config alone has not been validated as a drop-in for an unmodified partner
checkout; share this branch together with the config and this guide.

## Requirements

- Python 3 and `kubectl` on the control machine, with access to context `nautilus`.
- A namespace with `nextflow-sa` permitted to manage task Jobs and read their logs.
  The submission helper defaults to `braingeneers`; use `--namespace` to override it.
- The `rook-cephfs` storage class and capacity for a dedicated PVC. The synthetic
  default is 20 GiB; use `init --storage 200Gi`, for example, for larger inputs.
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

`run_workflow.py` defaults to the fixture test. For staged recordings, provide
`--input <PVC-directory> --run-name <name>`. Optional `--params-file <JSON>` and
`--config <config>` select stage settings and an additional NRP configuration.
The driver retains copies of both alongside its command, trace and logs. Resume
with the same run name, input, settings and `--resume`; never run two drivers in
one run directory.

The underlying `pipeline/main_multi_backend.nf` accepts these parameters:

| Parameter | Meaning |
| --- | --- |
| `--ecephys_path` | Input directory; defaults to NWB discovery, or use stage JSON for the generic reader described below. Falls back to `DATA_PATH`, then `DATA_DIR`. |
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

### Maxwell files and Maxwell-compressed NWBs

An NWB may retain Maxwell's HDF5 compression filter 401. Stage the pinned decoder
once on the shared workspace before launching tasks that read such inputs:

```bash
python3 scripts/nrp/submit.py --test-id "$TEST_ID" job decoder \
  --image base \
  --command 'python {source}/scripts/nrp/install_maxwell_plugin.py --directory {root}/runtime/maxwell-plugin'
```

After it succeeds, set `params.nrp_hdf5_plugin_path` to that directory in the
additional NRP config. This enables the decoder in every task's environment;
the existing processing images and NERSC configuration are unchanged. Check
actual sample reads with `inspect_recordings.py --kind maxwell|nwb --input <file>
--output <JSON>` in an NRP Job with `HDF5_PLUGIN_PATH` set. A registered filter
alone does not prove the source chunks can be decoded.

For a MaxTwo file with one recording per well, `prepare_maxwell.py --input <file>
--output <directory>` generates `params.json`, a complete well inventory and
probe geometry files for the dispatcher's existing `spikeinterface` loader.
It includes every well and the complete recording duration, and gives probes
distinct well names for NWB device/electrode grouping. Set the driver's
`--params-file` to this generated JSON and `--input` to the recording's directory.
The helper rejects multiple recording IDs rather than silently choosing one.

Size resources for the real inputs. For example, an additional NRP config can
override only selected resource fields without replacing the pinned image or
GPU selector:

```groovy
params.nrp_hdf5_plugin_path = "${System.getenv('TEST_ROOT')}/runtime/maxwell-plugin"
params.nrp_resources = [
    spikesort_kilosort4: [cpus: 4, memory: '16 GB', time: '3 h'],
    postprocessing: [memory: '16 GB', time: '3 h'],
    nwb_ecephys: [time: '3 h'],
    nwb_units: [time: '3 h']
]
```

These are example allowances, not production sizing guarantees. The pinned
postprocessing code's spike-location calculation builds a dense spatial grid;
its memory use depends on both channel count and the probe's physical extent.
An 8 GiB limit was insufficient for the 667 retained channels of a 3.83 × 2.00 mm
MEA, even though its sparse waveform array was small. Preserve the scientific
settings when increasing resource allowances for such recordings.

Input staging and durable artifact transfer are separate from the scientific
tasks. Stage S3 inputs on the PVC first and retain them for serialized recording
references. Large downloads can be faster through node-local scratch followed by
a sequential copy to the PVC. Give the transfer Job enough ephemeral storage.
Processing tasks currently rely on the shared workspace layout: serialized
recording descriptors can point back to the source through relative paths that
depend on the work-directory depth. A task using Nextflow `scratch` and
`stageInMode = 'copy'` needs descriptor remapping as well; copying its inputs
alone does not make these recordings portable.

Preprocessing can exit successfully while skipping a recording if its rejected
channel fraction exceeds `max_bad_channel_fraction` (default 0.5). Inspect its
channel labels and `error.txt` outputs before declaring a multiwell run complete.
Choose this whole-recording gate for the dataset separately from channel-quality
and unit-curation thresholds; sparse MEA wells may still have usable channels.

Sparse recordings can also lack enough isolated spikes to learn Kilosort's
initial templates. Its supported `templates_from_data: false` sorter setting
uses predefined templates, without lowering detection thresholds. This changes
the sorting configuration; select it explicitly for the dataset. The pinned
Kilosort version hardcodes the template-sampling stride, so changing `nskip`
only changes whitening and does not address this failure. To use predefined
templates, stage their pinned file before launching the workflow:

```bash
python3 scripts/nrp/submit.py --test-id "$TEST_ID" job templates \
  --image driver \
  --command 'python3 {source}/scripts/nrp/install_kilosort_templates.py --directory {root}/runtime/kilosort'
```

Set `env.KILOSORT_LOCAL_DOWNLOADS_PATH = "${System.getenv('TEST_ROOT')}/runtime/kilosort"`
in the additional NRP config. This also avoids Kilosort's missing-cache-directory
download error. Keep the chosen sorter settings and template provenance with the
run results; the shared defaults continue to learn templates from data.

Even with predefined templates, a well can yield no spikes at the selected
thresholds. The KS4 capsule accepts only its explicit completed-zero-detection
exception together with corroborating detector logs. It saves a zero-unit
sorting and `sorting_status.json`; the original log remains available. The
postprocessing capsule retains a real empty analyzer and recording reference,
and lists metrics that cannot be estimated without spikes. NWB export retains
silent wells' electrodes and LFP without adding unit rows. Reports distinguish
`no_detected_units` from `no_curated_units`; burst output is `no_spikes` in both
cases. Other sorter errors still fail. No detection or curation thresholds are
lowered automatically.

Visualization can still detect raw peaks for a drift plot when a zero-unit
analyzer has no spike locations. That fallback uses the original visualization
thresholds, which are separate from Kilosort detection. Its peaks are plot data,
not additional sorted units. `check_visualization.py --task-dir <finished-task>
--label <new-name> --expect-fallback` replays the full capsule with isolated
outputs at the same directory depth and checks that detection and trace plots
completed; run it in the base image with the decoder environment set.

The pinned units exporter omits unit waveform and unit-electrode columns when
combining wells with unequal original channel counts. Per-well analyzers retain
the waveforms; the combined NWB retains its electrode table, well identities,
spike times and LFP. Account for this existing export limitation when choosing
which artifacts to retain for downstream analysis.

For real recordings, run `validate_real.py analyzers --run-name <name>
--inventory <JSON>` in the base image, followed by `validate_nwb.py --validator
real --run-name <name> --inventory <JSON>` in the NWB image. Use the inventory
from `prepare_maxwell.py` or `inspect_recordings.py`. These checks require every
stream, full recording durations, matching analyzer/report/NWB spike trains,
and valid combined NWB exports. They do not measure biological sorting accuracy.

For a focused LFP regression, run `check_lfp_tail.py --kind maxwell|nwb --input
<file> --output <new-directory>` in the NWB image with 2 CPUs and the decoder
environment set. It checks saving and reading the last full second and partial
second at the exporter's chunk boundaries. `--stream` selects a Maxwell well.
An optional `--baseline <saved-tail>` compares the selected samples with an
earlier saved tail; it is unnecessary for ordinary full-workflow validation.

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
They also use the shared parameter-loading code to verify that changed stage
settings and capsule pins invalidate cached work while unchanged tasks remain
cached. Use `--label <new-name>` for repeated checks to retain earlier evidence.
The second downloads checksum-pinned Java 17 and Nextflow 23.08.0-edge into an
isolated ephemeral test cache, previews the shared workflow with the NERSC launch arguments,
and checks environment fallbacks and invalid JSON. Its tiny shell-only execution
tests explicitly disable Shifter. Results are under `validation/compatibility-*.json`;
commands, logs and task files are under `probes/compatibility/`.
Parameter-cache evidence is under `probes/parameter-cache-<label>/`.

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
