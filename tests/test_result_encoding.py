from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from ipaddress import ip_address
from uuid import UUID

import pytest

from query_man.result_encoding import ResultEncodingError, encode_result_value


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


def test_rejects_unsupported_values_and_non_string_object_keys() -> None:
    with pytest.raises(ResultEncodingError):
        encode_result_value(object())
    with pytest.raises(ResultEncodingError):
        encode_result_value({1: "not-json"})
