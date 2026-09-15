#!/usr/bin/env python3

from pathlib import Path
import json


def main():
    data_dir = Path("../data")
    results_dir = Path("../results")
    results_dir.mkdir(parents=True, exist_ok=True)

    input_files = sorted(
        str(path)
        for path in data_dir.rglob("*")
        if path.is_file()
    )

    summary = {
        "custom_block": "example_custom_block",
        "number_of_input_files": len(input_files),
        "input_files": input_files,
        "status": "success"
    }

    output_file = results_dir / "example_custom_block_summary.json"

    with output_file.open("w") as handle:
        json.dump(summary, handle, indent=2)

    print(f"Wrote {output_file}")


if __name__ == "__main__":
    main()
