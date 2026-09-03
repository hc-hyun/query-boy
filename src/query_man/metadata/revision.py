from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import Any

from query_man.guarded_query.result_encoding import CANONICAL_TIME_POLICY_MATERIAL
from query_man.metadata.models import CatalogSnapshot
from query_man.source_catalog.models import SourceProfile


def create_metadata_revision(source: SourceProfile, catalog: CatalogSnapshot) -> str:
    value = {
        "source_id": source.source_id,
        "source_name": source.name,
        "source_description": source.description,
        "view_contract_version": source.view_contract_version,
        "canonical_time_policy": CANONICAL_TIME_POLICY_MATERIAL,
        "allowed_schemas": source.allowed_schemas,
        "allowed_relation_kinds": source.allowed_relation_kinds,
        "execution_budget": source.budget,
        "relations": [
            {
                "schema": relation.schema,
                "name": relation.name,
                "kind": relation.kind,
                "comment": relation.comment,
                "definition_hash": relation.definition_hash,
                **({"security_invoker": True} if relation.security_invoker else {}),
                **({"security_barrier": True} if relation.security_barrier else {}),
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


def create_view_structure_signature(catalog: CatalogSnapshot) -> str:
    value = {
        "relations": [
            {
                "schema": relation.schema,
                "name": relation.name,
                "kind": relation.kind,
                "definition_hash": relation.definition_hash,
                "security_invoker": relation.security_invoker,
                "security_barrier": relation.security_barrier,
                "columns": [
                    {
                        "position": position,
                        "name": column.name,
                        "ordinal": column.ordinal,
                        "data_type": column.data_type,
                        "nullable": column.nullable,
                    }
                    for position, column in enumerate(relation.columns, 1)
                ],
            }
            for relation in catalog.relations
        ]
    }
    canonical = json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _canonicalize(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _canonicalize({field.name: getattr(value, field.name) for field in fields(value)})
    if isinstance(value, (list, tuple)):
        result = [_canonicalize(item) for item in value]
        return sorted(
            result,
            key=lambda item: json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )
    if isinstance(value, Mapping):
        return {key: _canonicalize(item) for key, item in sorted(value.items()) if item is not None}
    return value
