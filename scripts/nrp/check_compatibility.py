#!/usr/bin/env python3
"""NRP task-environment checks and NERSC runtime checks, run in finite NRP Jobs.

NERSC mode uses Java 17 / Nextflow 23.08.0-edge for previews and tiny shell-only
publication checks. It never runs Shifter or claims to validate NERSC execution.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

THREAD_KEYS = ("CO_CPUS", "N_JOBS_EXT", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
               "MKL_NUM_THREADS", "NUMBA_NUM_THREADS")
ROOT = Path(os.environ["TEST_ROOT"])
SOURCE = Path(os.environ["SOURCE_DIR"])
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("mode", choices=("nrp", "nersc"))
args = parser.parse_args()
folder = ROOT / "probes" / "compatibility" / args.mode
folder.mkdir(parents=True, exist_ok=True)
(ROOT / "validation").mkdir(exist_ok=True)
workflow = (SOURCE / "pipeline/main_multi_backend.nf").read_text()
# Exercise the actual shared publication declaration and default, not a copy.
publish = next(line.strip() for line in workflow.splitlines()
               if line.strip().startswith('publishDir "${params.results_path}",'))
default = next(line for line in workflow.splitlines()
               if line.startswith("params.publish_overwrite = "))
result = {"mode": args.mode, "nersc_shifter_executed": False,
          "source": json.loads((SOURCE / "source-manifest.json").read_text()),
          "checks": {}}
env = os.environ.copy()
command_prefix = ["nextflow"]


def run(name, command, cwd, expected_success=True):
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / f"{name}.command.json").write_text(json.dumps(command, indent=2))
    with (cwd / f"{name}.log").open("w") as log:
        completed = subprocess.run(command, cwd=cwd, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, timeout=1500)
    output = (cwd / f"{name}.log").read_text()
    print(f"{name}: exit {completed.returncode}\n{output[-5000:]}", flush=True)
    assert (completed.returncode == 0) == expected_success, (name, completed.returncode)
    return output


def nf(name, script, cwd, config, extra=(), expected_success=True):
    return run(name, [*command_prefix, "-log", str(cwd / f"{name}.nextflow.log"),
                     "-C", config, "run", str(script), *extra], cwd, expected_success)


def download(url, target, sha256):
    if not target.exists():
        with urllib.request.urlopen(url, timeout=120) as response, target.open("wb") as out:
            shutil.copyfileobj(response, out)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    assert digest == sha256, f"Checksum mismatch for {target.name}"
    return {"url": url, "sha256": digest, "bytes": target.stat().st_size}


if args.mode == "nersc":
    # The old Capsule launcher extracts many small files on first startup.
    # These disposable runtime files belong on local ephemeral storage, not CephFS.
    cache = Path("/tmp/mea-compatibility-runtime")
    cache.mkdir(parents=True, exist_ok=True)
    java_archive = cache / "java17.tar.gz"
    java = download(
        "https://github.com/adoptium/temurin17-binaries/releases/download/"
        "jdk-17.0.16%2B8/OpenJDK17U-jre_x64_linux_hotspot_17.0.16_8.tar.gz",
        java_archive, "2885b944da3793144d4a86a29524f4d7b68ba155f5c08afa444a3b40f7071892")
    java_home = cache / "jdk-17.0.16+8-jre"
    if not java_home.exists():
        with tarfile.open(java_archive) as bundle:
            bundle.extractall(cache, filter="data")
    executable = cache / "nextflow-23.08.0-edge-all"
    legacy = download(
        "https://github.com/nextflow-io/nextflow/releases/download/"
        "v23.08.0-edge/nextflow-23.08.0-edge-all",
        executable, "4f667c0ce1b939133baadd18678c83651e06f4bc676112af3b42d2c940ce5ac2")
    # The bundled launcher uses `which "$0"` to locate its embedded JAR.
    # Without executable permission it falls back to a broken relative path.
    executable.chmod(0o755)
    command_prefix = [str(executable)]
    for key in THREAD_KEYS:
        env.pop(key, None)
    env.pop("NXF_SYNTAX_PARSER", None)
    env.pop("DATA_PATH", None)
    env.update(JAVA_HOME=str(java_home), NXF_HOME=str(cache / "home"),
               NXF_DIST=str(cache / "framework"), NXF_PLUGINS_DIR=str(cache / "plugins"),
               NXF_VER="23.08.0-edge", NXF_ANSI_LOG="false",
               PROJECT_DIR=str(folder), DATA_DIR=str(ROOT / "data/fixture/signed"),
               RESULTS_PATH=str(folder / "preview-results"), TMPDIR=str(cache / "tmp"))
    (cache / "tmp").mkdir(exist_ok=True)
    result["runtime"] = {"java": java, "nextflow": legacy}
    run("java-version", [str(java_home / "bin/java"), "-version"], folder)
    run("nextflow-version", [*command_prefix, "-version"], folder)
    config = str(SOURCE / "pipeline/nextflow_nersc_template.config")
    resolved = run("nersc-config", [*command_prefix, "-C", config, "config", "-flat"], folder)
    assert "process.executor = 'local'" in resolved and "shifter.enabled = true" in resolved
    assert "beforeScript" not in resolved and "nf-k8s" not in resolved
    template_args = ["--input", "nwb", "--ecephys_path", env["DATA_DIR"],
                     "--runmode", "spikesort", "--n_jobs", "1",
                     "--preprocessing_args", "--motion skip --denoising cmr",
                     "--spikesorting_args", "--skip-motion-correction",
                     "--params_file", str(SOURCE / "scripts/params_no_motion.json"),
                     "--results_path", env["RESULTS_PATH"], "-preview"]
    output = nf("nersc-template-preview", SOURCE / "pipeline/main_multi_backend.nf",
                folder / "preview", config, template_args)
    assert "N JOBS: 1" in output and "Using SORTER: kilosort4" in output
    assert '"torch_device":"auto"' in output and '"do_correction":false' in output
    result["checks"]["nersc_template_preview"] = "passed"
    # Also exercise DATA_DIR fallback with no DATA_PATH or explicit input argument.
    nf("environment-fallback", SOURCE / "pipeline/main_multi_backend.nf",
       folder / "fallback", config, ["--params_file", str(SOURCE / "scripts/params_no_motion.json"),
                                     "--n_jobs", "1", "-preview"])
    malformed = folder / "invalid-stage.json"
    malformed.write_text('{"unknown_stage": {}}')
    output = nf("invalid-stage", SOURCE / "pipeline/main_multi_backend.nf",
                folder / "negative", config,
                ["--params_file", str(malformed), "-preview"], expected_success=False)
    assert "Unknown stage keys" in output
    result["checks"]["environment_fallback_and_invalid_stage"] = "passed"
    # These shell-only tests exercise Nextflow semantics, with Shifter disabled
    # explicitly in the test overlay because this is not a NERSC machine.
    overlay = folder / "shell-only.config"
    overlay.write_text("shifter.enabled = false\nprocess.container = null\n")
    test_config = config + "," + str(overlay)
else:
    env["NXF_HOME"] = str(ROOT / "cache" / "compatibility-nrp")
    test_config = str(SOURCE / "pipeline/nextflow_nrp.config")
    run("nextflow-version", [*command_prefix, "-version"], folder)
    script = folder / "threads.nf"
    script.write_text("""nextflow.enable.dsl = 2
params.results_path = null
""" + default + """
process THREAD_LIMITS {
    cpus { allocated_cpus }
    """ + publish + """
    input:
    val allocated_cpus
    output:
    path "limits_${allocated_cpus}.json"
    script:
    \"\"\"
    python3 - <<'PYTHON'
import json, os
from pathlib import Path
keys = "CO_CPUS N_JOBS_EXT OMP_NUM_THREADS OPENBLAS_NUM_THREADS MKL_NUM_THREADS NUMBA_NUM_THREADS".split()
values = {key: os.environ.get(key) for key in keys}
assert all(value == '${allocated_cpus}' for value in values.values()), values
Path('limits_${allocated_cpus}.json').write_text(json.dumps(values, indent=2))
print(values)
PYTHON
    \"\"\"
}
workflow { THREAD_LIMITS(Channel.of(1, 2, 4)) }
""")
    target = folder / "thread-results"
    nf("thread-limits", script, folder / "thread-run", test_config,
       ["--results_path", str(target), "-profile", "cpu",
        "-with-trace", str(folder / "threads.tsv")])
    result["checks"]["thread_limits"] = {
        str(cpus): json.loads((target / f"limits_{cpus}.json").read_text()) for cpus in (1, 2, 4)}
    # Small shell-only publication tests stay inside this finite driver Job.
    overlay = folder / "shell-only.config"
    overlay.write_text("process.executor = 'local'\nprocess.cpus = 1\n"
                       "process.container = null\ndocker.enabled = false\nshifter.enabled = false\n")
    test_config = str(overlay)
    for key in THREAD_KEYS:
        env.pop(key, None)

# Shared declaration: test fresh-run overwrite and cached-resume behavior with
# an intentionally modified published file, leaving the cached task untouched.
script = folder / "publication.nf"
script.write_text("""nextflow.enable.dsl = 2
params.results_path = null
""" + default + """
process PUBLICATION {
    """ + publish + """
    output:
    path 'artifact.txt'
    path 'environment.json'
    script:
    \"\"\"
    printf 'generated\\n' > artifact.txt
    python3 - <<'PYTHON'
import json, os
from pathlib import Path
keys = "CO_CPUS N_JOBS_EXT OMP_NUM_THREADS OPENBLAS_NUM_THREADS MKL_NUM_THREADS NUMBA_NUM_THREADS".split()
values = {key: os.environ.get(key) for key in keys}
assert all(value is None for value in values.values()), values
Path('environment.json').write_text(json.dumps(values, indent=2))
PYTHON
    \"\"\"
}
workflow { PUBLICATION() }
""")
for policy in ("default", "true", "false"):
    case = folder / f"publication-{policy}"
    published = case / "published"
    published.mkdir(parents=True, exist_ok=True)
    artifact = published / "artifact.txt"
    artifact.write_text("preexisting\n")
    extra = ["--results_path", str(published)]
    if policy != "default":
        extra += ["--publish_overwrite", policy]
    nf("initial", script, case, test_config, extra)
    initial = artifact.read_text()
    assert initial == ("preexisting\n" if policy == "false" else "generated\n"), (policy, initial)
    artifact.write_text("modified-after-run\n")
    nf("resume", script, case, test_config,
       [*extra, "-resume", "-with-trace", str(case / "resume.tsv")])
    resumed = artifact.read_text()
    assert resumed == ("generated\n" if policy == "true" else "modified-after-run\n"), (policy, resumed)
    assert "CACHED" in (case / "resume.tsv").read_text()
    result["checks"][f"publication_{policy}"] = {"initial": initial.strip(), "resume": resumed.strip(),
                                                   "task_cached": True, "no_thread_exports": True}

(ROOT / "validation" / f"compatibility-{args.mode}.json").write_text(json.dumps(result, indent=2))
print(json.dumps({"mode": args.mode, "checks": result["checks"]}, indent=2))
