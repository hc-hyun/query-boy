from __future__ import annotations

import base64
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from ipaddress import IPv4Address, IPv4Interface, IPv4Network, IPv6Address, IPv6Interface, IPv6Network
from uuid import UUID


class ResultEncodingError(ValueError):
    pass


def encode_result_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, bytes | bytearray | memoryview):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        return f"base64:{encoded}"
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(
        value,
        IPv4Address | IPv6Address | IPv4Interface | IPv6Interface | IPv4Network | IPv6Network | UUID,
    ):
        return str(value)
    if isinstance(value, Enum):
        return encode_result_value(value.value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ResultEncodingError("PostgreSQL result objects require string keys")
        return {key: encode_result_value(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [encode_result_value(item) for item in value]
    raise ResultEncodingError(f"Unsupported PostgreSQL result type: {type(value).__name__}")
