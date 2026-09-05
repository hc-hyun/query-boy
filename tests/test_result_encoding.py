from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

import query_man.guarded_query.result_encoding as result_encoding
from query_man.guarded_query.result_encoding import (
    CANONICAL_TIME_POLICY_MATERIAL,
    RESULT_OID_POLICY_MATERIAL,
    ResultEncodingError,
    encode_result_value,
)


def test_encodes_launch_postgres_scalars_as_stable_json_values() -> None:
    encoded = encode_result_value(
        {
            "integer": 123,
            "text": "한글🙂",
            "null": None,
            "numeric": Decimal("12345678901234567890.1234567890"),
            "timestamp": datetime(2026, 8, 23, 1, 2, 3, 456789, tzinfo=UTC),
            "date": date(2026, 8, 23),
        }
    )

    assert encoded == {
        "integer": 123,
        "text": "한글🙂",
        "null": None,
        "numeric": "12345678901234567890.1234567890",
        "timestamp": "2026-08-23T01:02:03.456789+00:00",
        "date": "2026-08-23",
    }
    json.dumps(encoded, allow_nan=False)


def test_normalizes_aware_datetimes_and_dst_values_to_utc() -> None:
    encoded = encode_result_value(
        {
            "seoul": datetime(
                2026,
                8,
                25,
                12,
                34,
                tzinfo=ZoneInfo("Asia/Seoul"),
            ),
            "new_york_spring": datetime(
                2024,
                3,
                10,
                3,
                tzinfo=ZoneInfo("America/New_York"),
            ),
            "new_york_fall": datetime(
                2024,
                11,
                3,
                1,
                30,
                0,
                123456,
                tzinfo=ZoneInfo("America/New_York"),
                fold=1,
            ),
        }
    )

    assert encoded == {
        "seoul": "2026-08-25T03:34:00+00:00",
        "new_york_spring": "2024-03-10T07:00:00+00:00",
        "new_york_fall": "2024-11-03T06:30:00.123456+00:00",
    }


def test_preserves_naive_datetime_and_date_isoformat() -> None:
    values = [
        datetime(2026, 8, 25, 1, 2, 3, 456789),
        date(2026, 8, 25),
    ]

    assert [encode_result_value(value) for value in values] == [value.isoformat() for value in values]


def test_canonical_time_policy_material_is_exact_and_immutable() -> None:
    assert dict(CANONICAL_TIME_POLICY_MATERIAL) == {
        "version": 1,
        "reader_session_timezone": "UTC",
        "aware_datetime": "utc_isoformat_plus_00_00",
        "naive_datetime": "preserve_isoformat",
        "date": "preserve_isoformat",
        "time": "preserve_isoformat",
        "timetz": "preserve_isoformat",
    }
    with pytest.raises(TypeError):
        CANONICAL_TIME_POLICY_MATERIAL["version"] = 2  # type: ignore[index]


def test_result_oid_policy_material_is_exact_and_recursively_immutable() -> None:
    assert dict(RESULT_OID_POLICY_MATERIAL) == {
        "version": 1,
        "postgresql_major": 18,
        "allowed_scalar_oids": (
            ("int8", 20),
            ("int2", 21),
            ("int4", 23),
            ("text", 25),
            ("date", 1082),
            ("timestamptz", 1184),
            ("numeric", 1700),
        ),
    }
    with pytest.raises(TypeError):
        RESULT_OID_POLICY_MATERIAL["version"] = 2  # type: ignore[index]
    with pytest.raises(TypeError):
        RESULT_OID_POLICY_MATERIAL["allowed_scalar_oids"][0] = (  # type: ignore[index]
            "bool",
            16,
        )


@pytest.mark.parametrize("oid", [20, 21, 23, 25, 1082, 1184, 1700])
def test_result_oid_gate_accepts_each_launch_scalar_oid(oid: int) -> None:
    result_encoding._require_supported_result_oids((oid,))


@pytest.mark.parametrize(
    "oids",
    [
        pytest.param((16,), id="bool"),
        pytest.param((701,), id="float8"),
        pytest.param((17,), id="bytea"),
        pytest.param((1083,), id="time"),
        pytest.param((1266,), id="timetz"),
        pytest.param((1186,), id="interval"),
        pytest.param((2950,), id="uuid"),
        pytest.param((869,), id="inet"),
        pytest.param((1007,), id="int4-array"),
        pytest.param((3802,), id="jsonb"),
        pytest.param((), id="empty"),
        pytest.param(("20",), id="malformed"),
        pytest.param((True,), id="bool-is-not-int-oid"),
    ],
)
def test_result_oid_gate_rejects_with_one_bounded_error(
    oids: tuple[object, ...],
) -> None:
    with pytest.raises(ResultEncodingError) as captured:
        result_encoding._require_supported_result_oids(oids)

    assert str(captured.value) == "Unsupported PostgreSQL result type"
    assert not any(str(value) in str(captured.value) for value in oids)


def test_result_oid_gate_bounds_malformed_iterators() -> None:
    def malformed_oids() -> object:
        yield 20
        raise RuntimeError("private result description")

    with pytest.raises(ResultEncodingError) as captured:
        result_encoding._require_supported_result_oids(malformed_oids())

    assert str(captured.value) == "Unsupported PostgreSQL result type"
    assert captured.value.__cause__ is None


def test_rejects_unsupported_values_and_non_string_object_keys() -> None:
    with pytest.raises(ResultEncodingError):
        encode_result_value(object())
    with pytest.raises(ResultEncodingError):
        encode_result_value({1: "not-json"})
