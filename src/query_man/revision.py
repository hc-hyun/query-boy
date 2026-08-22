from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
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
        "semantic_overlay": asdict(source.semantic_overlay),
        "relations": [
            {
                "schema": relation.schema,
                "name": relation.name,
                "kind": relation.kind,
                "comment": relation.comment,
                "definition_hash": relation.definition_hash,
                **({"security_invoker": True} if relation.security_invoker else {}),
                **({"primary_key": relation.primary_key} if relation.primary_key else {}),
                **(
                    {"foreign_keys": [asdict(key) for key in relation.foreign_keys]}
                    if relation.foreign_keys
                    else {}
                ),
                **(
                    {"indexes": [asdict(index) for index in relation.indexes]}
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
    if isinstance(value, list):
        result = [_canonicalize(item) for item in value]
        return sorted(
            result,
            key=lambda item: json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )
    if isinstance(value, dict):
        return {key: _canonicalize(item) for key, item in sorted(value.items()) if item is not None}
    return value
