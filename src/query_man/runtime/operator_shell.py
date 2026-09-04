from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from dotenv import dotenv_values

from query_man.source_catalog.registry import (
    RegistryConfigurationError,
    SourceRegistry,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qm",
        description="Validate Query Man source packages.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root (default: current directory)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    source = commands.add_parser("source", help="source package operations")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    source_commands.add_parser(
        "validate",
        help="validate local source.yaml, views.sql, and budget files",
    )
    return parser


def _environment(root: Path) -> dict[str, str]:
    values = {
        key: value
        for key, value in dotenv_values(root / ".env").items()
        if value is not None
    }
    values.update(os.environ)
    return values


def validate_sources(root: Path) -> dict[str, object]:
    root = root.resolve()
    source_directory = root / "config" / "sources"
    budget_file = root / "config" / "budget-profiles.yaml"
    database_file = root / "config" / "database-profiles.yaml"
    if source_directory.is_symlink() or budget_file.is_symlink() or database_file.is_symlink():
        raise RegistryConfigurationError("Configuration symlinks are not allowed")
    registry = SourceRegistry.load(
        source_directory,
        budget_file,
        database_file,
        Path("/run/secrets/query-man/databases"),
        _environment(root),
    )
    source_ids = sorted(registry.source_ids())
    return {
        "status": "valid",
        "source_directory": "config/sources",
        "budget_file": "config/budget-profiles.yaml",
        "database_file": "config/database-profiles.yaml",
        "source_count": len(source_ids),
        "source_ids": source_ids,
        "live_database_checked": False,
    }


def run_main(arguments: list[str]) -> int:
    parsed = _parser().parse_args(arguments)
    try:
        result = validate_sources(parsed.root)
    except (OSError, UnicodeDecodeError, RegistryConfigurationError, yaml.YAMLError):
        print(
            "Source package validation failed. Check config/sources and "
            "config/database-profiles.yaml and config/budget-profiles.yaml.",
            file=sys.stderr,
        )
        return 1
    print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False).rstrip())
    return 0


def main() -> None:
    raise SystemExit(run_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
