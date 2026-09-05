#!/usr/bin/env python3
"""Validate versioned Query Man source packages without reading runtime credentials."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from query_man.source_catalog.registry import RegistryConfigurationError, SourceRegistry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate versioned Query Man source packages.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository or isolated configuration root (default: current directory)",
    )
    return parser


def validate(root: Path) -> dict[str, object]:
    resolved_root = root.resolve()
    source_directory = resolved_root / "config" / "sources"
    budget_file = resolved_root / "config" / "budget-profiles.yaml"
    database_file = resolved_root / "config" / "database-profiles.yaml"
    if source_directory.is_symlink() or budget_file.is_symlink() or database_file.is_symlink():
        raise RegistryConfigurationError("configuration symlinks are not allowed")

    registry = SourceRegistry.load(
        source_directory,
        budget_file,
        database_file,
        Path("/run/secrets/query-man/databases"),
        {},
    )
    source_ids = sorted(registry.source_ids())
    return {
        "status": "valid",
        "validation_scope": "configuration_and_package_layout",
        "views_sql_validated": False,
        "source_count": len(source_ids),
        "source_ids": source_ids,
        "runtime_environment_read": False,
        "database_connected": False,
        "credential_files_read": False,
    }


def run(arguments: list[str]) -> int:
    parsed = _parser().parse_args(arguments)
    try:
        result = validate(parsed.root)
    except (OSError, UnicodeDecodeError, RegistryConfigurationError, yaml.YAMLError):
        print(
            "Source package validation failed; no configuration value or credential detail was printed.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
