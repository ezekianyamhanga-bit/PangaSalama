import argparse
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATABASE = Path(os.getenv("DATABASE_PATH", str(ROOT / "pangasalama.sqlite3")))
REQUIRED_TABLES = {"listings", "requests", "notifications"}


def verify_database(path):
    database = Path(path)
    if not database.is_file():
        raise ValueError(f"Database file does not exist: {database}")
    connection = sqlite3.connect(str(database), timeout=30)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError(f"Database integrity check failed: {result[0] if result else 'no result'}")
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = REQUIRED_TABLES - tables
        if missing:
            raise ValueError(f"Database is missing required tables: {', '.join(sorted(missing))}")
    finally:
        connection.close()
    return database


def _copy_database(source, destination):
    source_connection = sqlite3.connect(str(source), timeout=30)
    destination_connection = sqlite3.connect(str(destination), timeout=30)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def backup_database(database, destination):
    source = Path(database)
    target = Path(destination)
    if source.resolve() == target.resolve():
        raise ValueError("Backup destination must differ from the database path")
    if not source.is_file():
        raise ValueError(f"Database file does not exist: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        with target.open("xb"):
            pass
    except FileExistsError as error:
        raise FileExistsError(f"Backup already exists; choose a new path: {target}") from error

    temporary_path = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        _copy_database(source, temporary_path)
        verify_database(temporary_path)
        os.replace(temporary_path, target)
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


def restore_database(database, backup):
    target = Path(database)
    source = verify_database(backup)
    if source.resolve() == target.resolve():
        raise ValueError("Restore source must differ from the database path")
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        if Path(str(target) + suffix).exists():
            raise RuntimeError(
                f"Cannot safely restore while SQLite sidecar exists: {target}{suffix}. "
                "Stop the application and checkpoint the database first.")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.restore.", suffix=".tmp", dir=str(target.parent))
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        _copy_database(source, temporary_path)
        verify_database(temporary_path)

        safety_backup = None
        if target.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safety_backup = backup_database(target, target.with_name(
                f"{target.name}.pre-restore-{stamp}.sqlite3"))
        os.replace(temporary_path, target)
        return safety_backup
    finally:
        temporary_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Back up, verify, or restore the PangaSalama SQLite database.")
    commands = parser.add_subparsers(dest="command", required=True)
    backup_parser = commands.add_parser("backup", help="Create a consistent SQLite backup")
    backup_parser.add_argument("destination", type=Path)
    verify_parser = commands.add_parser("verify", help="Check database integrity and required tables")
    verify_parser.add_argument("path", nargs="?", type=Path, default=DATABASE)
    restore_parser = commands.add_parser("restore", help="Restore a backup after stopping the application")
    restore_parser.add_argument("backup", type=Path)
    args = parser.parse_args()

    if args.command == "backup":
        print(f"Backup created: {backup_database(DATABASE, args.destination)}")
    elif args.command == "verify":
        print(f"Database verified: {verify_database(args.path)}")
    else:
        safety_backup = restore_database(DATABASE, args.backup)
        if safety_backup:
            print(f"Pre-restore safety backup created: {safety_backup}")
        print(f"Database restored from: {args.backup}")


if __name__ == "__main__":
    main()
