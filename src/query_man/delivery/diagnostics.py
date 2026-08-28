from __future__ import annotations

from typing import Protocol

from query_man.delivery.access import CallerContext


class DiagnosticCapture(Protocol):
    def capture_question(
        self,
        caller: CallerContext,
        source_id: str,
        question: str,
    ) -> None: ...

    def capture_sql(
        self,
        caller: CallerContext,
        source_id: str,
        sql: str,
        query_id: str,
    ) -> None: ...
