from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from query_man.metadata.models import CatalogColumn, CatalogRelation, CatalogSnapshot
from query_man.source_catalog.registry import SourceRegistry

ROOT_DIRECTORY = Path(__file__).resolve().parents[1]
QUERY_CAVE_DIRECTORY = ROOT_DIRECTORY / "query-cave"
QUERY_CAVE_CONFIG_DIRECTORY = QUERY_CAVE_DIRECTORY / "config"
DUMMY_ENVIRONMENT = {
    "QUERY_CAVE_POSTGRES_PORT": "55432",
}


def load_test_registry(environment: Mapping[str, str] = DUMMY_ENVIRONMENT) -> SourceRegistry:
    return SourceRegistry.load(
        QUERY_CAVE_CONFIG_DIRECTORY / "sources",
        QUERY_CAVE_CONFIG_DIRECTORY / "budget-profiles.yaml",
        QUERY_CAVE_CONFIG_DIRECTORY / "database-profiles.yaml",
        Path("/run/secrets/query-man/databases"),
        environment,
    )


def column(name: str, data_type: str = "text") -> CatalogColumn:
    return CatalogColumn(name, f'"{name}"', 1, data_type, "unknown")


def relation(
    qualified_name: str,
    columns: Sequence[CatalogColumn],
    kind: str = "view",
) -> CatalogRelation:
    schema, name = qualified_name.split(".", 1)
    return CatalogRelation(
        schema=schema,
        name=name,
        qualified_name=qualified_name,
        sql_name=f'"{schema}"."{name}"',
        kind=kind,  # type: ignore[arg-type]
        columns=tuple(
            replace(item, ordinal=index) for index, item in enumerate(columns, 1)
        ),
        view_contract_source="query-cave",
        view_contract_version=1,
    )


def minimal_query_cave_snapshot() -> CatalogSnapshot:
    return CatalogSnapshot(
        (
            relation(
                "signal_schema.case_notes_view",
                [
                    column("note_id", "bigint"),
                    column("case_id", "bigint"),
                    column("author_code"),
                    column("note"),
                    column("noted_at", "timestamp with time zone"),
                    column("note_type"),
                ],
            ),
            relation(
                "signal_schema.case_files_view",
                [
                    column("case_id", "bigint"),
                    column("reported_at", "timestamp with time zone"),
                    column("reporter_code"),
                    column("responder_code"),
                    column("summary"),
                    column("assessment"),
                    column("response"),
                    column("device_version"),
                    column("software_version"),
                    column("note_count", "integer"),
                    column("case_type"),
                    column("severity"),
                    column("status"),
                ],
            ),
            relation(
                "signal_schema.response_units_view",
                [
                    column("unit_id", "bigint"),
                    column("commissioned_on", "date"),
                    column("unit_code"),
                    column("case_count", "integer"),
                    column("open_case_count", "integer"),
                ],
            ),
        )
    )
