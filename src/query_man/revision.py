from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from typing import Any

from query_man.models import CatalogSnapshot, SourceProfile


def create_metadata_revision(source: SourceProfile, catalog: CatalogSnapshot) -> str:
    value = {
        "source_id": source.source_id,
        "allowed_schemas": source.allowed_schemas,
        "allowed_relation_kinds": source.allowed_relation_kinds,
        **(
            {"tenant_isolation": source.tenant_isolation}
            if source.tenant_isolation != "none"
            else {}
        ),
        "execution_budget": source.budget,
        "semantic_overlay": source.semantic_overlay,
        "relations": [
            {
                "schema": relation.schema,
                "name": relation.name,
                "kind": relation.kind,
                "comment": relation.comment,
                "definition_hash": relation.definition_hash,
                **({"security_invoker": True} if relation.security_invoker else {}),
                **(
                    {"primary_key": _ordered_names(relation.primary_key)}
                    if relation.primary_key
                    else {}
                ),
                **(
                    {
                        "foreign_keys": [
                            {
                                "columns": _ordered_names(key.columns),
                                "referenced_relation": key.referenced_relation,
                                "referenced_columns": _ordered_names(
                                    key.referenced_columns
                                ),
                            }
                            for key in relation.foreign_keys
                        ]
                    }
                    if relation.foreign_keys
                    else {}
                ),
                **(
                    {
                        "indexes": [
                            {
                                "columns": _ordered_names(index.columns),
                                "unique": index.unique,
                                "primary": index.primary,
                            }
                            for index in relation.indexes
                        ]
                    }
                    if relation.indexes
                    else {}
                ),
                "columns": [
                    {
                        "name": column.name,
                        "ordinal": column.ordinal,
                        "data_type": column.data_type,
                        "nullable": column.nullable,
                        "comment": column.comment,
                    }
                    for column in relation.columns
                ],
            }
            for relation in catalog.relations
        ],
    }
    canonical = json.dumps(_canonicalize(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _canonicalize(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _canonicalize(
            {field.name: getattr(value, field.name) for field in fields(value)}
        )
    if isinstance(value, (list, tuple)):
        result = [_canonicalize(item) for item in value]
        return sorted(
            result,
            key=lambda item: json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )
    if isinstance(value, Mapping):
        return {key: _canonicalize(item) for key, item in sorted(value.items()) if item is not None}
    return value


def _ordered_names(values: Sequence[str]) -> list[dict[str, str]]:
    return [{f"{position:04d}": value} for position, value in enumerate(values)]
