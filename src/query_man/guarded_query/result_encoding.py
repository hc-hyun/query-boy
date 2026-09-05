from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType


class ResultEncodingError(ValueError):
    pass


CANONICAL_TIME_POLICY_MATERIAL: Mapping[str, object] = MappingProxyType(
    {
        "version": 1,
        "reader_session_timezone": "UTC",
        "aware_datetime": "utc_isoformat_plus_00_00",
        "naive_datetime": "preserve_isoformat",
        "date": "preserve_isoformat",
        "time": "preserve_isoformat",
        "timetz": "preserve_isoformat",
    }
)

_RESULT_OID_PAIRS = (
    ("int8", 20),
    ("int2", 21),
    ("int4", 23),
    ("text", 25),
    ("date", 1082),
    ("timestamptz", 1184),
    ("numeric", 1700),
)

RESULT_OID_POLICY_MATERIAL: Mapping[str, object] = MappingProxyType(
    {
        "version": 1,
        "postgresql_major": 18,
        "allowed_scalar_oids": _RESULT_OID_PAIRS,
    }
)

_ALLOWED_RESULT_OIDS = frozenset(oid for _type_name, oid in _RESULT_OID_PAIRS)
_RESULT_OID_POLICY_ERROR = "Unsupported PostgreSQL result type"


def _require_supported_result_oids(oids: Iterable[object]) -> None:
    try:
        result_oids = tuple(oids)
    except Exception:
        raise ResultEncodingError(_RESULT_OID_POLICY_ERROR) from None
    if not result_oids or any(
        type(oid) is not int or oid not in _ALLOWED_RESULT_OIDS for oid in result_oids
    ):
        raise ResultEncodingError(_RESULT_OID_POLICY_ERROR)


def encode_result_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.utcoffset() is not None:
            return value.astimezone(UTC).isoformat()
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ResultEncodingError("PostgreSQL result objects require string keys")
        return {key: encode_result_value(item) for key, item in value.items()}
    raise ResultEncodingError(f"Unsupported PostgreSQL result type: {type(value).__name__}")
