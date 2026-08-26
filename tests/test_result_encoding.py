from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from ipaddress import ip_address
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

import query_man.result_encoding as result_encoding
from query_man.result_encoding import (
    CANONICAL_TIME_POLICY_MATERIAL,
    RESULT_OID_POLICY_MATERIAL,
    ResultEncodingError,
    encode_result_value,
)


def test_encodes_exact_and_binary_postgres_scalars_as_stable_strings() -> None:
    encoded = encode_result_value(
        {
            "numeric": Decimal("12345678901234567890.1234567890"),
            "binary": b"\xff\x00",
            "timestamp": datetime(2026, 8, 23, 1, 2, 3, 456789, tzinfo=UTC),
            "date": date(2026, 8, 23),
            "time": time(1, 2, 3, 456789),
            "interval": timedelta(days=-1, microseconds=1),
            "uuid": UUID("00000000-0000-0000-0000-000000000001"),
            "inet": ip_address("2001:db8::1"),
        }
    )

    assert encoded == {
        "numeric": "12345678901234567890.1234567890",
        "binary": "base64:/wA=",
        "timestamp": "2026-08-23T01:02:03.456789+00:00",
        "date": "2026-08-23",
        "time": "01:02:03.456789",
        "interval": "-1 day, 0:00:00.000001",
        "uuid": "00000000-0000-0000-0000-000000000001",
        "inet": "2001:db8::1",
    }
    json.dumps(encoded, allow_nan=False)


def test_encodes_non_finite_floats_without_nonstandard_json_numbers() -> None:
    assert encode_result_value([float("nan"), float("inf"), float("-inf")]) == [
        "NaN",
        "Infinity",
        "-Infinity",
    ]


def test_normalizes_aware_datetimes_and_nested_dst_values_to_utc() -> None:
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
            "new_york_dst": [
                datetime(
                    2024,
                    3,
                    10,
                    3,
                    tzinfo=ZoneInfo("America/New_York"),
                ),
                datetime(
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
            ],
        }
    )

    assert encoded == {
        "seoul": "2026-08-25T03:34:00+00:00",
        "new_york_dst": [
            "2024-03-10T07:00:00+00:00",
            "2024-11-03T06:30:00.123456+00:00",
        ],
    }


def test_preserves_naive_datetime_date_time_and_timetz_isoformat() -> None:
    values = [
        datetime(2026, 8, 25, 1, 2, 3, 456789),
        date(2026, 8, 25),
        time(1, 2, 3, 456789),
        time(1, 2, 3, 456789, tzinfo=timezone(timedelta(hours=9))),
    ]

    assert encode_result_value(values) == [value.isoformat() for value in values]


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
