from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from query_man.metadata_store import decode_snapshot, encode_snapshot
from query_man.models import CatalogForeignKey, CatalogIndex
from query_man.revision import create_metadata_revision
from tests.helpers import load_test_registry, minimal_development_snapshot


def test_snapshot_codec_preserves_legacy_json_and_freezes_decoded_graph() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    snapshot = replace(
        snapshot,
        relations=tuple(
            replace(
                relation,
                primary_key=("issue_id",)
                if relation.qualified_name == "ai.issue_overview"
                else relation.primary_key,
                indexes=(
                    CatalogIndex(
                        ("discovered_at",), unique=False, primary=False
                    ),
                )
                if relation.qualified_name == "ai.issue_overview"
                else relation.indexes,
                foreign_keys=(
                    CatalogForeignKey(
                        ("issue_id",), "ai.issue_overview", ("issue_id",)
                    ),
                )
                if relation.qualified_name == "ai.issue_comments"
                else relation.foreign_keys,
            )
            for relation in snapshot.relations
        ),
    )
    revision = create_metadata_revision(source, snapshot)
    encoded = encode_snapshot(snapshot)
    legacy_json = json.loads(json.dumps(encoded))

    decoded = decode_snapshot(source, revision, legacy_json)

    assert encode_snapshot(decoded.snapshot) == encoded
    assert create_metadata_revision(source, decoded.snapshot) == revision
    relations = encoded["relations"]
    assert isinstance(relations, list)
    by_name = {
        f"{relation['schema_name']}.{relation['relation_name']}": relation
        for relation in relations
    }
    assert by_name["ai.issue_overview"]["primary_key"] == ["issue_id"]
    assert by_name["ai.issue_overview"]["indexes"][0]["columns"] == [
        "discovered_at"
    ]
    assert by_name["ai.issue_comments"]["foreign_keys"][0]["columns"] == [
        "issue_id"
    ]
    assert by_name["ai.issue_comments"]["foreign_keys"][0][
        "referenced_columns"
    ] == ["issue_id"]
    assert isinstance(decoded.snapshot.relations, tuple)
    assert isinstance(decoded.snapshot.relations[0].columns, tuple)

    legacy_json["relations"][0]["comment"] = "mutated after decode"
    assert decoded.snapshot.relations[0].comment != "mutated after decode"


def test_minimal_snapshot_json_matches_pre_immutability_golden() -> None:
    encoded = json.dumps(
        encode_snapshot(minimal_development_snapshot()),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    assert len(encoded) == 2_609
    assert hashlib.sha256(encoded).hexdigest() == (
        "1f392b10b1b505430920e95d07549ed2dbc51e20cece984a4528e2abf406dbc7"
    )
