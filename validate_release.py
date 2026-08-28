#!/usr/bin/env python3
"""Validate private and public StatusPulse release artifacts."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from release_contract import (
    ReleaseValidationError,
    inspect_database,
    stage_public_artifacts,
    validate_public_artifacts,
    validate_release,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    database = commands.add_parser(
        "database", help="validate a canonical SQLite database"
    )
    database.add_argument("database", type=Path)
    database.add_argument("--minimum-rows", type=int, default=1)
    database.add_argument("--minimum-sources", type=int, default=1)

    release = commands.add_parser(
        "release", help="cross-check a complete generated release"
    )
    release.add_argument("--database", type=Path, required=True)
    release.add_argument("--stats", type=Path, required=True)
    release.add_argument("--mttr-stats", type=Path, required=True)
    release.add_argument("--sample", type=Path, required=True)
    release.add_argument("--full-csv", type=Path, required=True)
    release.add_argument("--minimum-rows", type=int, required=True)
    release.add_argument("--minimum-sources", type=int, default=1)

    public = commands.add_parser(
        "public", help="validate an exact GitHub Pages artifact allowlist"
    )
    public.add_argument("directory", type=Path)

    stage_public = commands.add_parser(
        "stage-public", help="construct and validate a GitHub Pages artifact"
    )
    stage_public.add_argument("source", type=Path)
    stage_public.add_argument("destination", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "database":
            summary = inspect_database(
                arguments.database,
                minimum_rows=arguments.minimum_rows,
                minimum_sources=arguments.minimum_sources,
            )
            print(summary.row_count)
        elif arguments.command == "release":
            summary = validate_release(
                database_path=arguments.database,
                stats_path=arguments.stats,
                mttr_stats_path=arguments.mttr_stats,
                sample_path=arguments.sample,
                full_csv_path=arguments.full_csv,
                minimum_rows=arguments.minimum_rows,
                minimum_sources=arguments.minimum_sources,
            )
            print(
                f"validated {summary.row_count} incidents across "
                f"{summary.source_count} sources"
            )
        elif arguments.command == "public":
            total, sample_rows = validate_public_artifacts(arguments.directory)
            print(
                f"validated public allowlist: total={total}, sample_rows={sample_rows}"
            )
        else:
            total, sample_rows = stage_public_artifacts(
                arguments.source, arguments.destination
            )
            print(f"staged public allowlist: total={total}, sample_rows={sample_rows}")
    except (ReleaseValidationError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
