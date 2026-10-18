"""Verify an account ZIP and restore it into a NEW, isolated local data directory."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.backups import BackupError, restore_account_backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True, help="New absolute path; existing paths are rejected")
    parser.add_argument("--user-id", required=True, help="Must exactly match the backup owner")
    args = parser.parse_args()
    try:
        result = restore_account_backup(args.archive, args.destination, user_id=args.user_id)
    except (BackupError, OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Account backup restoration failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
