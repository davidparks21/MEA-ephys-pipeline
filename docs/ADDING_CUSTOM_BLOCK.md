# Adding a Custom Analysis Block

This pipeline supports adding custom analysis blocks without modifying the upstream Allen Neural Dynamics repositories.

Custom blocks can be implemented in two ways:

1. Local capsules stored under `capsules/`
2. External GitHub capsules pinned to an exact commit in `pipeline/capsule_versions.env`

Existing local examples include:

- `capsules/report_generation/`
- `capsules/burst_detection/`

## 1. Create a local capsule

Create a directory such as:

    capsules/my_custom_block/

At minimum:

    capsules/my_custom_block/
    └── run_capsule.py

The script should read staged inputs from `../data/` and write outputs to `../results/`.

An example template is provided in:

    capsules/example_custom_block/

## 2. Add a Nextflow process

Add the process to:

    pipeline/main_multi_backend.nf

Use the existing `report_generation` or `burst_detection` processes as examples.

A basic pattern is:

    process my_custom_block {

        tag 'my-custom-block'

        input:
        path input_data, stageAs: 'capsule/data/*'

        output:
        path 'capsule/results/*', emit: results

        script:
        """
        set -e

        mkdir -p capsule/data
        mkdir -p capsule/results
        mkdir -p capsule/code

        cp -r <local-capsule-path>/. capsule/code/

        cd capsule/code
        python run_capsule.py
        """
    }

Adapt the input channel according to which upstream stage should feed the block.

## 3. Connect the block in the workflow

Call the process from the `workflow` section of:

    pipeline/main_multi_backend.nf

For example:

    my_custom_block_out = my_custom_block(
        postprocessing_out.results.collect()
    )

The exact input channel depends on the upstream process.

## 4. Add it to the NERSC configuration

Edit:

    pipeline/nextflow_nersc_template.config

Add:

    withName: my_custom_block {
        maxForks = 1
    }

CPU, memory, GPU, and other process-specific settings can also be configured here.

## 5. External GitHub capsule

A larger custom block can live in its own GitHub repository.

Add a pinned commit to:

    pipeline/capsule_versions.env

For example:

    MY_CUSTOM_BLOCK=<git_commit_sha>

Then clone that exact version from the Nextflow process:

    clone_repo "https://github.com/USERNAME/my-custom-block.git" "${versions['MY_CUSTOM_BLOCK']}"

Pinning the commit makes the pipeline reproducible and prevents changes on a branch such as `main` from silently changing a run.

## 6. Outputs

Custom blocks should normally write outputs under:

    capsule/results/

The Nextflow process can publish them into a descriptive pipeline results directory such as:

    results/my_custom_block/

## 7. Testing

Before running the custom block on a complete dataset:

1. Test the capsule script independently.
2. Run it on a small dataset.
3. Confirm the expected outputs are produced.
4. Confirm the process exits successfully.
5. Run the full pipeline with `-resume`.

Nextflow will reuse completed upstream stages when possible.

## Local versus external capsules

Use a local capsule when the analysis is small and specific to this pipeline.

Use an external repository when the analysis has its own dependencies, development history, or may be reused by other pipelines.
