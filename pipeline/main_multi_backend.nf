#!/usr/bin/env nextflow
nextflow.enable.dsl = 2

params.ecephys_path = System.getenv('DATA_PATH') ?: System.getenv('DATA_DIR')
params.results_path = System.getenv('RESULTS_PATH') ?: "${launchDir}/results"
params.params_file = null
params.torch_device = null

// Git repository prefix - can be overridden via command line or environment variable
params.git_repo_prefix = System.getenv('GIT_REPO_PREFIX') ?: 'https://github.com/AllenNeuralDynamics/aind-'

// Helper function for git cloning
def gitCloneFunction = '''
clone_repo() {
    local repo_url="$1"
    local commit_hash="$2"

    echo "cloning git repo: \${repo_url} (commit: \${commit_hash})..."

    git clone "\${repo_url}" capsule-repo
    git -C capsule-repo -c core.fileMode=false checkout "\${commit_hash}" --quiet

    mv capsule-repo/code capsule/code
    rm -rf capsule-repo
}
'''

if (!params.ecephys_path) {
    error 'Set --ecephys_path to a directory containing an NWB recording (or set DATA_PATH / DATA_DIR).'
}
println "DATA_PATH: ${params.ecephys_path}"
println "RESULTS_PATH: ${params.results_path}"

// Load parameters from JSON file if provided
def json_params = [:]
if (params.params_file) {
    json_params = new groovy.json.JsonSlurper().parseText(new File(params.params_file).text)
    if (!(json_params instanceof Map)) {
        error '--params_file must contain a JSON object keyed by pipeline stage.'
    }
    def unknown = json_params.keySet() - ['job_dispatch', 'preprocessing', 'spikesorting', 'postprocessing', 'curation', 'visualization', 'nwb']
    if (unknown) {
        error "Unknown stage keys in --params_file: ${unknown}. See scripts/params_no_motion.json for the nested format."
    }
    println "Loaded parameters from ${params.params_file}"
}

// get commit hashes for capsules
params.capsule_versions = "${baseDir}/capsule_versions.env"
def versions = [:]
file(params.capsule_versions).eachLine { line ->
    def (key, value) = line.tokenize('=')
    versions[key] = value
}

// container tag
params.container_tag = "si-${versions['SPIKEINTERFACE_VERSION']}"
println "CONTAINER TAG: ${params.container_tag}"

params_keys = params.keySet()

// if not specified, assume local executor
if (!params_keys.contains('executor')) {
    params.executor = "local"
}
// set global n_jobs for local executor
if (params.executor == "local") 
{
    if ("n_jobs" in params_keys) {
        n_jobs = params.n_jobs
    }
    else {
        n_jobs = -1
    }
    println "N JOBS: ${n_jobs}"
    job_args=" --n-jobs ${n_jobs}"
}
else {
    job_args=""
}

// set runmode
if ("runmode" in params_keys) {
    runmode = params.runmode
}
else {
    runmode = "full"
}
println "Using RUNMODE: ${runmode}"

if (params.params_file) {
    println "Using parameters from JSON file: ${params.params_file}"
} else {
    println "No parameters file provided, using command line arguments."
}

// Initialize args variables with params from JSON file or command line args
def job_dispatch_args = ""
if (params.params_file && json_params.job_dispatch) {
    job_dispatch_args = "--params '${groovy.json.JsonOutput.toJson(json_params.job_dispatch)}'"
} else if ("job_dispatch_args" in params_keys && params.job_dispatch_args instanceof String) {
    job_dispatch_args = params.job_dispatch_args
}

def preprocessing_args = ""
if (params.params_file && json_params.preprocessing) {
    preprocessing_args = "--params '${groovy.json.JsonOutput.toJson(json_params.preprocessing)}'"
} else if ("preprocessing_args" in params_keys && params.preprocessing_args instanceof String) {
    preprocessing_args = params.preprocessing_args
}

def postprocessing_args = ""
if (params.params_file && json_params.postprocessing) {
    postprocessing_args = "--params '${groovy.json.JsonOutput.toJson(json_params.postprocessing)}'"
} else if ("postprocessing_args" in params_keys && params.postprocessing_args instanceof String) {
    postprocessing_args = params.postprocessing_args
}

def curation_args = ""
if (params.params_file && json_params.curation) {
    curation_args = "--params '${groovy.json.JsonOutput.toJson(json_params.curation)}'"
} else if ("curation_args" in params_keys && params.curation_args instanceof String) {
    curation_args = params.curation_args
}

def visualization_kwargs = ""
if (params.params_file && json_params.visualization) {
    visualization_kwargs = "--params '${groovy.json.JsonOutput.toJson(json_params.visualization)}'"
} else if ("visualization_kwargs" in params_keys && params.visualization_kwargs instanceof String) {
    visualization_kwargs = params.visualization_kwargs
}

def nwb_ecephys_args = ""
if (params.params_file && json_params.nwb?.ecephys) {
    nwb_ecephys_args = "--params '${groovy.json.JsonOutput.toJson(json_params.nwb.ecephys)}'"
} else if ("nwb_ecephys_args" in params_keys && params.nwb_ecephys_args instanceof String) {
    nwb_ecephys_args = params.nwb_ecephys_args
}

// For spikesorting, use the parameters for the selected sorter
def sorter = null
if (params.params_file && json_params.spikesorting) {
    sorter = json_params.spikesorting.sorter ?: null
}

sorter = sorter ?: params.get('sorter', 'kilosort4')

def spikesorting_args = ""
if (params.params_file && json_params.spikesorting) {
    def sorter_params = json_params.spikesorting[sorter]
    if (sorter_params) {
        spikesorting_args = "--params '${groovy.json.JsonOutput.toJson(sorter_params)}'"
    }
} else if ("spikesorting_args" in params_keys && params.spikesorting_args instanceof String) {
    spikesorting_args = params.spikesorting_args
} else if ("spikesorting_args" in params_keys) {
    error 'Use --spikesorting_args="--flag" (with an equals sign), or the nested --params_file format.'
}

if (sorter == null) {
    println "No sorter specified, defaulting to kilosort4"
    sorter = "kilosort4"
}

// Force an explicit device for CPU/GPU comparisons. Keep the pinned capsule defaults.
if (params.torch_device) {
    if (sorter != 'kilosort4' || !(params.torch_device in ['cpu', 'cuda'])) {
        error '--torch_device must be cpu or cuda and requires --sorter kilosort4.'
    }
    if (params.get('spikesorting_args', null)) {
        error 'Use --params_file with --torch_device so the selected device cannot be overridden by raw arguments.'
    }
    def settings = json_params.spikesorting?.kilosort4 ?:
        new groovy.json.JsonSlurper().parseText(file("${baseDir}/kilosort4_defaults.json").text)
    settings.sorter.torch_device = params.torch_device
    spikesorting_args = "--params '${groovy.json.JsonOutput.toJson(settings)}'"
}

println "Using SORTER: ${sorter} with args: ${spikesorting_args}"

if (runmode == 'fast'){
    preprocessing_args = "--motion skip"
    postprocessing_args = "--skip-extensions spike_locations,principal_components"
    nwb_ecephys_args = "--skip-lfp"
    println "Running in fast mode. Setting parameters:"
    println "preprocessing_args: ${preprocessing_args}"
    println "postprocessing_args: ${postprocessing_args}"
    println "nwb_ecephys_args: ${nwb_ecephys_args}"
}

// Process definitions
def container_name = "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base:${params.container_tag}"

process job_dispatch {
    tag 'job-dispatch'
    container container_name

    input:
    path input_folder, stageAs: 'capsule/data/ecephys_session'
    
    output:
    path 'capsule/results/*', emit: results
    path 'max_duration.txt', emit: max_duration_file  // file containing the value


    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
    fi

    TASK_DIR=\$(pwd)

    echo "[${task.tag}] cloning git repo..."
    ${gitCloneFunction}
    clone_repo "${params.git_repo_prefix}ephys-job-dispatch.git" "${versions['JOB_DISPATCH']}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run

    ./run --input nwb ${job_dispatch_args}


    MAX_DURATION_MIN=\$(python get_max_recording_duration_min.py)

    cd \$TASK_DIR
    echo "\$MAX_DURATION_MIN" > max_duration.txt

    echo "[${task.tag}] completed!"

    """
}

process preprocessing {
    tag 'preprocessing'
    container "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base:${params.container_tag}"

    input:
    path local_code, stageAs: 'capsule-source'
    val max_duration_minutes
    path ecephys_session_input, stageAs: 'capsule/data/ecephys_session'
    path job_dispatch_results, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*', emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
        # Make sure N_JOBS matches allocated CPUs on SLURM
        export N_JOBS_EXT=${task.cpus}
    fi

    echo "[${task.tag}] staging local capsule code..."
    cp -rL capsule-source capsule/code
    # Vendored at the original pin; see capsules/preprocessing/UPSTREAM.md.

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    bash run ${preprocessing_args} ${job_args}

    echo "[${task.tag}] completed!"
    """
}

process spikesort_kilosort25 {
    tag 'spikesort-kilosort25'
    container "ghcr.io/allenneuraldynamics/aind-ephys-spikesort-kilosort25:${params.container_tag}"

    input:
    val max_duration_minutes
    path preprocessing_results, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*', emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
        # Make sure N_JOBS matches allocated CPUs on SLURM
        export N_JOBS_EXT=${task.cpus}
    fi

    echo "[${task.tag}] cloning git repo..."
    ${gitCloneFunction}
    clone_repo "${params.git_repo_prefix}ephys-spikesort-kilosort25.git" "${versions['SPIKESORT_KS25']}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run ${spikesorting_args} ${job_args}

    echo "[${task.tag}] completed!"
    """
}

process spikesort_kilosort4 {
    tag 'spikesort-kilosort4'
    container "ghcr.io/allenneuraldynamics/aind-ephys-spikesort-kilosort4:${params.container_tag}"

    input:
    val max_duration_minutes
    path preprocessing_results, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*', emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
        # Make sure N_JOBS matches allocated CPUs on SLURM
        export N_JOBS_EXT=${task.cpus}
    fi

    echo "[${task.tag}] cloning git repo..."
    ${gitCloneFunction}
    clone_repo "${params.git_repo_prefix}ephys-spikesort-kilosort4.git" "${versions['SPIKESORT_KS4']}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run ${spikesorting_args} ${job_args}

    echo "[${task.tag}] completed!"
    """
}

process spikesort_spykingcircus2 {
    tag 'spikesort-spykingcircus2'
    container "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base:${params.container_tag}"

    input:
    val max_duration_minutes
    path preprocessing_results, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*', emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
        # Make sure N_JOBS matches allocated CPUs on SLURM
        export N_JOBS_EXT=${task.cpus}
    fi

    echo "[${task.tag}] cloning git repo..."
    ${gitCloneFunction}
    clone_repo "${params.git_repo_prefix}ephys-spikesort-spykingcircus2.git" "${versions['SPIKESORT_SC2']}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run ${spikesorting_args} ${job_args}

    echo "[${task.tag}] completed!"
    """
}

process spikesort_lupin {
    tag 'spikesort-lupin'
    container "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base:${params.container_tag}"

    input:
    val max_duration_minutes
    path preprocessing_results, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*', emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
        # Make sure N_JOBS matches allocated CPUs on SLURM
        export N_JOBS_EXT=${task.cpus}
    fi

    echo "[${task.tag}] cloning git repo..."
    ${gitCloneFunction}
    clone_repo "${params.git_repo_prefix}ephys-spikesort-lupin.git" "${versions['SPIKESORT_LUPIN']}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run ${spikesorting_args} ${job_args}

    echo "[${task.tag}] completed!"
    """
}

process postprocessing {
    tag 'postprocessing'
    container "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base:${params.container_tag}"

    input:
    val max_duration_minutes
    path ecephys_session_input, stageAs: 'capsule/data/ecephys_session'
    path job_dispatch_results, stageAs: 'capsule/data/*'
    path preprocessing_results, stageAs: 'capsule/data/*'
    path spikesort_results, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*', emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
        # Make sure N_JOBS matches allocated CPUs on SLURM
        export N_JOBS_EXT=${task.cpus}
    fi

    echo "[${task.tag}] cloning git repo..."
    ${gitCloneFunction}
    clone_repo "${params.git_repo_prefix}ephys-postprocessing.git" "${versions['POSTPROCESSING']}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run ${postprocessing_args} ${job_args}

    echo "[${task.tag}] completed!"
    """
}

process curation {
    tag 'curation'
    container "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base:${params.container_tag}"

    input:
    val max_duration_minutes
    path postprocessing_results, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*', emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
        # Make sure N_JOBS matches allocated CPUs on SLURM
        export N_JOBS_EXT=${task.cpus}
    fi

    echo "[${task.tag}] cloning git repo..."
    ${gitCloneFunction}
    clone_repo "https://github.com/Varda006/aind-ephys-curation.git" "${versions['CURATION']}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run ${curation_args} ${job_args}

    echo "[${task.tag}] completed!"
    """
}

process visualization {
    tag 'visualization'
    container "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base:${params.container_tag}"

    input:
    val max_duration_minutes
    path ecephys_session_input, stageAs: 'capsule/data/ecephys_session'
    path job_dispatch_results, stageAs: 'capsule/data/*'
    path preprocessing_results, stageAs: 'capsule/data/*'
    path spikesort_results, stageAs: 'capsule/data/*'
    path postprocessing_results, stageAs: 'capsule/data/*'
    path curation_results, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*', emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
        # Make sure N_JOBS matches allocated CPUs on SLURM
        export N_JOBS_EXT=${task.cpus}
    fi

    echo "[${task.tag}] cloning git repo..."
    ${gitCloneFunction}
    clone_repo "${params.git_repo_prefix}ephys-visualization.git" "${versions['VISUALIZATION']}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run ${visualization_kwargs}

    echo "[${task.tag}] completed!"
    """
}

process results_collector {
    tag 'result-collector'
    container "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base:${params.container_tag}"

    publishDir "${params.results_path}", saveAs: { filename -> new File(filename).getName() }, mode: 'copy', overwrite: true

    input:
    val max_duration_minutes
    path ecephys_session_input, stageAs: 'capsule/data/ecephys_session'
    path job_dispatch_results, stageAs: 'capsule/data/*'
    path preprocessing_results, stageAs: 'capsule/data/*'
    path spikesort_results, stageAs: 'capsule/data/*'
    path postprocessing_results, stageAs: 'capsule/data/*'
    path curation_results, stageAs: 'capsule/data/*'
    path visualization_results, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*', emit: results
    path 'capsule/results/*', emit: nwb_data
    path 'capsule/results/*', emit: qc_data

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
    fi

    echo "[${task.tag}] cloning git repo..."
    ${gitCloneFunction}
    clone_repo "${params.git_repo_prefix}ephys-results-collector.git" "${versions['RESULTS_COLLECTOR']}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run --pipeline-data-path ${params.ecephys_path} --pipeline-results-path ${params.results_path}

    echo "[${task.tag}] completed!"
    """
}

process quality_control {
    tag 'quality-control'
    container "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base:${params.container_tag}"

    input:
    val max_duration_minutes
    path ecephys_session_input, stageAs: 'capsule/data/ecephys_session'
    path job_dispatch_results, stageAs: 'capsule/data/*'
    path results_data, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*', emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
        # Make sure N_JOBS matches allocated CPUs on SLURM
        export N_JOBS_EXT=${task.cpus}
    fi

    echo "[${task.tag}] cloning git repo..."
    ${gitCloneFunction}
    clone_repo "${params.git_repo_prefix}ephys-processing-qc.git" "${versions['QUALITY_CONTROL']}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run

    echo "[${task.tag}] completed!"
    """
}

process quality_control_collector {
    tag 'qc-collector'
    container "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base:${params.container_tag}"

    publishDir "${params.results_path}", saveAs: { filename -> new File(filename).getName() }, mode: 'copy', overwrite: true

    input:
    val max_duration_minutes
    path quality_control_results, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*'

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
    fi

    echo "[${task.tag}] cloning git repo..."
    ${gitCloneFunction}
    clone_repo "${params.git_repo_prefix}ephys-qc-collector.git" "${versions['QUALITY_CONTROL_COLLECTOR']}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run

    echo "[${task.tag}] completed!"
    """
}


process nwb_ecephys {
    tag 'nwb-ecephys'
    container "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-nwb:${params.container_tag}"

    input:
    path local_code, stageAs: 'capsule-source'
    val max_duration_minutes
    path ecephys_session_input, stageAs: 'capsule/data/ecephys_session'
    path job_dispatch_results, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*', emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
        # Make sure N_JOBS matches allocated CPUs on SLURM
        export N_JOBS_EXT=${task.cpus}
    fi

    echo "[${task.tag}] staging local capsule code..."
    cp -rL capsule-source capsule/code
    # Vendored at the original pin; see capsules/nwb_ecephys/UPSTREAM.md.

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    bash run ${nwb_ecephys_args}

    echo "[${task.tag}] completed!"
    """
}

process nwb_units {
    tag 'nwb-units'
    container "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-nwb:${params.container_tag}"

    publishDir "${params.results_path}/nwb", saveAs: { filename -> new File(filename).getName() }, mode: 'copy', overwrite: true

    input:
    val max_duration_minutes
    path ecephys_session_input, stageAs: 'capsule/data/ecephys_session'
    path job_dispatch_results, stageAs: 'capsule/data/*'
    path results_data, stageAs: 'capsule/data/*'
    path nwb_ecephys_results, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*'

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    export MKL_NUM_THREADS=${task.cpus} NUMBA_NUM_THREADS=${task.cpus}

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    echo "[${task.tag}] cloning git repo..."
    ${gitCloneFunction}
    clone_repo "${params.git_repo_prefix}units-nwb.git" "${versions['NWB_UNITS']}"

    if [[ ${params.executor} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
    fi

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run

    echo "[${task.tag}] completed!"
    """
}

process report_generation {
    tag { "report-generation:${recording_id}" }
    container "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base:${params.container_tag}"
    publishDir "${params.results_path}/reports", saveAs: { filename -> new File(filename).getName() }, mode: 'copy', overwrite: true

    input:
    tuple val(recording_id), path(analyzer, stageAs: 'capsule/data/analyzer.zarr')
    path report_code, stageAs: 'capsule/code'

    output:
    tuple val(recording_id), path('capsule/results/*'), emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    mkdir -p 'capsule/results/${recording_id}'
    python -m pip install openpyxl==3.1.5 --no-deps -q --no-cache-dir --target capsule/pydeps
    python -m pip install et-xmlfile==2.0.0 --no-deps -q --no-cache-dir --target capsule/pydeps
    export PYTHONPATH="\$(pwd)/capsule/pydeps:\${PYTHONPATH:-}"
    python capsule/code/run_capsule.py \
        --analyzer-dir capsule/data/analyzer.zarr \
        --output-dir 'capsule/results/${recording_id}' \
        --thresholds '{"firing_rate": 0.1, "presence_ratio": 0.8}'
    """
}

process burst_detection {
    tag { "burst-detection:${recording_id}" }
    container "ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base:${params.container_tag}"
    publishDir "${params.results_path}/bursts", saveAs: { filename -> new File(filename).getName() }, mode: 'copy', overwrite: true

    input:
    tuple val(recording_id), path(spike_times, stageAs: 'capsule/data/spike_times.npy')
    path burst_code, stageAs: 'capsule/code'

    output:
    tuple val(recording_id), path('capsule/results/*'), emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e
    export CO_CPUS=${task.cpus} N_JOBS_EXT=${task.cpus}
    export OMP_NUM_THREADS=${task.cpus} OPENBLAS_NUM_THREADS=${task.cpus}
    mkdir -p 'capsule/results/${recording_id}'
    python capsule/code/run_capsule.py \
        --spike-times capsule/data/spike_times.npy \
        --output-dir 'capsule/results/${recording_id}' \
        --plot-mode separate
    """
}

workflow {
    // Input channel from ecephys path
    ecephys_ch = Channel.fromPath(params.ecephys_path, type: 'dir', checkIfExists: true)

    // Job dispatch
    job_dispatch_out = job_dispatch(ecephys_ch.collect())

    max_duration_file = job_dispatch_out.max_duration_file
    max_duration_minutes = max_duration_file.map { it.text.trim() }
    max_duration_minutes.view { "Max recording duration: ${it}min" }

    // Preprocessing
    preprocessing_out = preprocessing(
        Channel.value(file("${baseDir}/../capsules/preprocessing/code")),
        max_duration_minutes,
        ecephys_ch.collect(),
        job_dispatch_out.results.flatten()
    )

    // Spike sorting based on selected sorter
    // def spikesort
    if (sorter == 'kilosort25') {
        spikesort_out = spikesort_kilosort25(
            max_duration_minutes,
            preprocessing_out.results
        )
    } else if (sorter == 'kilosort4') {
        spikesort_out = spikesort_kilosort4(
            max_duration_minutes,
            preprocessing_out.results
        )
    } else if (sorter == 'spykingcircus2') {
        spikesort_out = spikesort_spykingcircus2(
            max_duration_minutes,
            preprocessing_out.results
        )
    } else if (sorter == 'lupin') {
        spikesort_out = spikesort_lupin(
            max_duration_minutes,
            preprocessing_out.results
        )
    } else {
        error "Unsupported sorter: ${sorter}"
    }

    // Postprocessing
    postprocessing_out = postprocessing(
        max_duration_minutes,
        ecephys_ch.collect(),
        job_dispatch_out.results.flatten(),
        preprocessing_out.results.collect(),
        spikesort_out.results.collect()
    )

    // Curation
    curation_out = curation(
        max_duration_minutes,
        postprocessing_out.results
    )

    // Visualization
    visualization_out = visualization(
        max_duration_minutes,
        ecephys_ch.collect(),
        job_dispatch_out.results.collect(),
        preprocessing_out.results,
        spikesort_out.results.collect(),
        postprocessing_out.results.collect(),
        curation_out.results.collect()
    )

    // Results collection
    results_collector_out = results_collector(
        max_duration_minutes,
        ecephys_ch.collect(),
        job_dispatch_out.results.collect(),
        preprocessing_out.results.collect(),
        spikesort_out.results.collect(),
        postprocessing_out.results.collect(),
        curation_out.results.collect(),
        visualization_out.results.collect()
    )




    // Stage the code in this checkout and select one analyzer explicitly per recording.
    analyzer_ch = postprocessing_out.results.flatten()
        .filter { it.name.startsWith('postprocessed_') && it.name.endsWith('.zarr') }
        .map { analyzer -> tuple(analyzer.name.replaceFirst(/^postprocessed_/, '').replaceFirst(/\.zarr$/, ''), analyzer) }
    report_generation_out = report_generation(
        analyzer_ch,
        Channel.value(file("${baseDir}/../capsules/report_generation"))
    )
    burst_detection(
        report_generation_out.results.map { id, folder -> tuple(id, folder.resolve('spike_times.npy')) },
        Channel.value(file("${baseDir}/../capsules/burst_detection"))
    )
    // Quality control disabled for NERSC debug run
    // Reason: QC currently fails on unsigned raw data during highpass filtering.
    // quality_control_out = quality_control(
    //     max_duration_minutes,
    //     ecephys_ch.collect(),
    //     job_dispatch_out.results.flatten(),
    //     results_collector_out.qc_data.collect()
    // )
    //
    // quality_control_collector(
    //     max_duration_minutes,
    //     quality_control_out.results.collect()
    // )

    // Quality control
//     quality_control_out = quality_control(
//         max_duration_minutes,
//         ecephys_ch.collect(),
//         job_dispatch_out.results.flatten(),
//         results_collector_out.qc_data.collect()
//     )
// 
//     // Quality control collection
//     quality_control_collector(
//         max_duration_minutes,
//         quality_control_out.results.collect()
//     )

    // NWB ecephys
    nwb_ecephys_out = nwb_ecephys(
        Channel.value(file("${baseDir}/../capsules/nwb_ecephys/code")),
        max_duration_minutes,
        ecephys_ch.collect(),
        job_dispatch_out.results.collect(),
    )

    // NWB units
    nwb_units(
        max_duration_minutes,
        ecephys_ch.collect(),
        job_dispatch_out.results.collect(),
        results_collector_out.nwb_data.collect(),
        nwb_ecephys_out.results.collect()
    )
}
