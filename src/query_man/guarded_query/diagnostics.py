from __future__ import annotations

from pglast import ast, parse_sql
from pglast.parser import ParseError
from pglast.stream import RawStream


def redact_sql_literals(sql: str, *, max_sql_bytes: int = 100_000) -> str | None:
    """Return a parsed SQL rendering with every constant replaced by NULL.

    The result is diagnostic text, not executable SQL. Invalid or oversized input has no
    safe rendering and returns ``None``.
    """

    if not sql.strip() or len(sql.encode("utf-8")) > max_sql_bytes:
        return None
    try:
        statements = parse_sql(sql)
    except ParseError:
        return None
    if len(statements) != 1 or not isinstance(statements[0].stmt, ast.SelectStmt):
        return None
    _remove_constants(statements)
    rendered = RawStream()(statements)
    return rendered if len(rendered.encode("utf-8")) <= max_sql_bytes else None


def _remove_constants(value: object) -> None:
    if isinstance(value, ast.A_Const):
        value.val = None
        value.isnull = True
        return
    if isinstance(value, tuple):
        for child in value:
            _remove_constants(child)
        return
    if isinstance(value, ast.Node):
        for attribute in value:
            _remove_constants(getattr(value, attribute))
