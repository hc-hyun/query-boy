from __future__ import annotations


def is_json_content_type(value: str | None) -> bool:
    if value is None or not 1 <= len(value) <= 128:
        return False
    segments = value.split(";")
    if segments[0].strip().casefold() != "application/json":
        return False
    if len(segments) == 1:
        return True
    if len(segments) != 2:
        return False
    name, separator, raw_value = segments[1].strip().partition("=")
    if separator != "=" or name.strip().casefold() != "charset":
        return False
    charset = raw_value.strip()
    if charset.startswith('"') or charset.endswith('"'):
        if len(charset) < 2 or not (charset.startswith('"') and charset.endswith('"')):
            return False
        charset = charset[1:-1]
    return charset.casefold() == "utf-8"
