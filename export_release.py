#!/usr/bin/env python3
"""Export the private StatusPulse SQLite release as a deterministic CSV."""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from release_contract import (
    INCIDENT_COLUMNS,
    ReleaseValidationError,
    inspect_connection,
    safe_csv_cell,
)


def export_release(database_path: Path, destination: Path) -> int:
    """Atomically export all incident rows and return the exported row count."""
    source = database_path.resolve()
    target = destination.resolve()
    if source == target:
        raise ReleaseValidationError("database and CSV destination must differ")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    row_count = 0
    columns = ", ".join(INCIDENT_COLUMNS)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            newline="",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            writer = csv.writer(output)
            writer.writerow(INCIDENT_COLUMNS)
            with closing(
                sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
            ) as database:
                database.execute("BEGIN")
                summary = inspect_connection(database)
                rows = database.execute(
                    f"SELECT {columns} FROM incidents ORDER BY started_at DESC, id ASC"
                )
                writer.writerows(
                    tuple(safe_csv_cell(value) for value in row) for row in rows
                )
                row_count = summary.row_count
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    except (OSError, sqlite3.Error) as error:
        raise ReleaseValidationError(f"cannot export release CSV: {error}") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return row_count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args(argv)
    try:
        row_count = export_release(arguments.database, arguments.destination)
    except ReleaseValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"exported {row_count} incidents to {arguments.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
