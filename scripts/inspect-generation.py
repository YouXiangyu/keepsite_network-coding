"""Print private aggregate generation state from an existing local database."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.generation.observability import inspect_queue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="Existing absolute business data directory")
    args = parser.parse_args()
    try:
        result = inspect_queue(args.data_dir)
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(1, "Cannot inspect generation state: check directory, schema and database availability.\n")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
