# Example Custom Block

This directory is a minimal template for adding a local analysis block to the MEA ephys pipeline.

The capsule expects Nextflow to stage inputs under:

    capsule/data/

The script runs from:

    capsule/code/

and writes outputs under:

    capsule/results/

See `docs/ADDING_CUSTOM_BLOCK.md` for instructions on connecting a custom block to `main_multi_backend.nf`.
