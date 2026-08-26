from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import query_man.assurance_cli as assurance_cli
from query_man.quality import QualityGateError, QualityReport
from tests.helpers import ROOT_DIRECTORY


def test_console_scripts_target_the_assurance_cli_composition_root() -> None:
    configuration = tomllib.loads(
        (ROOT_DIRECTORY / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert configuration["project"]["scripts"]["query-man-evaluate"] == (
        "query_man.assurance_cli:evaluate_main"
    )
    assert configuration["project"]["scripts"]["query-man-verify"] == (
        "query_man.assurance_cli:verify_main"
    )


@pytest.mark.parametrize(
    ("command", "entrypoint", "description"),
    (
        (
            "query-man-evaluate",
            assurance_cli.evaluate_main,
            "Evaluate metadata retrieval quality gates",
        ),
        (
            "query-man-verify",
            assurance_cli.verify_main,
            "Verify versioned golden SQL against live sources",
        ),
    ),
)
def test_cli_help_contract_is_unchanged(
    command: str,
    entrypoint: Any,
    description: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", [command, "--help"])

    with pytest.raises(SystemExit) as captured:
        entrypoint()

    assert captured.value.code == 0
    assert capsys.readouterr().out == (
        f"usage: {command} [-h] [--root ROOT]\n\n"
        f"{description}\n\n"
        "options:\n"
        "  -h, --help   show this help message and exit\n"
        "  --root ROOT\n"
    )


@pytest.mark.parametrize(
    ("command", "entrypoint", "runner_name"),
    (
        ("query-man-evaluate", assurance_cli.evaluate_main, "_run_evaluation"),
        ("query-man-verify", assurance_cli.verify_main, "_run_verification"),
    ),
)
def test_cli_root_argument_is_resolved_before_running(
    command: str,
    entrypoint: Any,
    runner_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots: list[Path] = []

    async def record(root: Path) -> None:
        roots.append(root)

    supplied = tmp_path / "nested" / ".."
    monkeypatch.setattr(sys, "argv", [command, "--root", str(supplied)])
    monkeypatch.setattr(assurance_cli, runner_name, record)

    entrypoint()

    assert roots == [supplied.resolve()]


@pytest.mark.asyncio
async def test_evaluation_cli_preserves_bootstrap_paths_output_exit_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[object] = []
    registry = SimpleNamespace(list=lambda: [{"source_id": "known-source"}])
    catalog = SimpleNamespace(close=_recording_close(events, "catalog"))
    report = QualityReport(1, 1.0, 1.0, 1_024, 1_024, ())

    class Evaluation:
        async def evaluate(self, metadata: object) -> QualityReport:
            events.append(("evaluate", metadata))
            return report

    class Verified:
        def revision_map(self) -> dict[str, frozenset[str]]:
            return {"known-source": frozenset({"sha256:revision"})}

    monkeypatch.setenv("QUERY_MAN_SOURCE_MODE", "managed")
    monkeypatch.setattr(
        assurance_cli,
        "load_dotenv",
        lambda path: events.append(("dotenv", path)),
    )
    monkeypatch.setattr(
        assurance_cli,
        "SourceRegistry",
        SimpleNamespace(
            load=lambda sources, budgets: events.append(
                ("registry", sources, budgets)
            )
            or registry
        ),
    )
    monkeypatch.setattr(
        assurance_cli,
        "QualityEvaluation",
        SimpleNamespace(
            load=lambda path, known: events.append(("quality", path, known))
            or Evaluation()
        ),
    )
    monkeypatch.setattr(
        assurance_cli,
        "VerifiedQueryRegistry",
        SimpleNamespace(
            load=lambda path, known: events.append(("verified", path, known))
            or Verified()
        ),
    )
    monkeypatch.setattr(assurance_cli, "PostgresCatalog", _static_launch_catalog(catalog))
    monkeypatch.setattr(
        assurance_cli,
        "MetadataService",
        lambda source_reader, provider, **keywords: (
            "metadata",
            source_reader,
            provider,
            keywords,
        ),
    )

    await assurance_cli._run_evaluation(tmp_path)

    assert capsys.readouterr().out == (
        '{"answerability_recall": 1.0, "average_context_bytes": 1024, '
        '"case_count": 1, "failures": [], "max_context_bytes": 1024, '
        '"relation_accuracy": 1.0, "status": "ok"}\n'
    )
    assert ("dotenv", tmp_path / ".env") in events
    assert (
        "registry",
        tmp_path / "config" / "sources",
        tmp_path / "config" / "budget-profiles.yaml",
    ) in events
    assert (
        "quality",
        tmp_path / "config" / "quality-evaluation.yaml",
        {"known-source"},
    ) in events
    assert (
        "verified",
        tmp_path / "config" / "verified-queries.yaml",
        {"known-source"},
    ) in events
    assert events[-1] == "catalog"


@pytest.mark.asyncio
async def test_evaluation_cli_keeps_failed_report_and_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[object] = []
    report = QualityReport(1, 0.0, 1.0, 1_024, 1_024, ("relation mismatch",))

    class Evaluation:
        async def evaluate(self, _metadata: object) -> QualityReport:
            raise QualityGateError(report)

    _patch_evaluation_composition(monkeypatch, Evaluation(), events)

    with pytest.raises(SystemExit) as captured:
        await assurance_cli._run_evaluation(tmp_path)

    assert captured.value.code == 1
    assert capsys.readouterr().out == (
        '{"answerability_recall": 1.0, "average_context_bytes": 1024, '
        '"case_count": 1, "failures": ["relation mismatch"], '
        '"max_context_bytes": 1024, "relation_accuracy": 0.0, '
        '"status": "failed"}\n'
    )
    assert events == ["catalog"]


@pytest.mark.asyncio
async def test_verify_cli_composes_guarded_query_path_and_preserves_cleanup_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[object] = []
    paths: list[object] = []
    registry = SimpleNamespace(list=lambda: [{"source_id": "known-source"}])
    catalog = SimpleNamespace(close=_recording_close(events, "catalog"))
    executor = SimpleNamespace(close=_recording_close(events, "executor"))
    metadata = object()

    class Service:
        def __init__(self, source_reader: object, prepared: object, adapter: object) -> None:
            events.append(("query-service", source_reader, prepared, adapter))

    class Verified:
        async def verify_all(
            self,
            prepared: object,
            service: object,
        ) -> list[dict[str, object]]:
            events.append(("verify-all", prepared, service))
            return [{"query_id": "verified-query", "row_count": 1}]

    monkeypatch.setenv("QUERY_MAN_SOURCE_MODE", "managed")
    monkeypatch.setattr(
        assurance_cli,
        "load_dotenv",
        lambda path: paths.append(("dotenv", path)),
    )
    monkeypatch.setattr(
        assurance_cli,
        "SourceRegistry",
        SimpleNamespace(
            load=lambda sources, budgets: paths.append(
                ("registry", sources, budgets)
            )
            or registry
        ),
    )
    monkeypatch.setattr(
        assurance_cli,
        "VerifiedQueryRegistry",
        SimpleNamespace(
            load=lambda path, known: paths.append(("verified", path, known))
            or Verified()
        ),
    )
    monkeypatch.setattr(assurance_cli, "PostgresCatalog", _static_launch_catalog(catalog))
    monkeypatch.setattr(assurance_cli, "PostgresQueryExecutor", lambda: executor)
    monkeypatch.setattr(assurance_cli, "MetadataService", lambda _registry, _catalog: metadata)
    monkeypatch.setattr(assurance_cli, "QueryService", Service)

    await assurance_cli._run_verification(tmp_path)

    assert capsys.readouterr().out == (
        '{"status": "ok", "verified": '
        '[{"query_id": "verified-query", "row_count": 1}]}\n'
    )
    assert events[0] == ("query-service", registry, metadata, executor)
    assert events[1][0] == "verify-all"
    assert events[-2:] == ["executor", "catalog"]
    assert paths == [
        ("dotenv", tmp_path / ".env"),
        (
            "registry",
            tmp_path / "config" / "sources",
            tmp_path / "config" / "budget-profiles.yaml",
        ),
        (
            "verified",
            tmp_path / "config" / "verified-queries.yaml",
            {"known-source"},
        ),
    ]


@pytest.mark.asyncio
async def test_verify_cli_propagates_failure_without_success_output_and_still_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[object] = []
    registry = SimpleNamespace(list=lambda: [{"source_id": "known-source"}])
    catalog = SimpleNamespace(close=_recording_close(events, "catalog"))
    executor = SimpleNamespace(close=_recording_close(events, "executor"))

    class Verified:
        async def verify_all(
            self,
            _metadata: object,
            _service: object,
        ) -> list[dict[str, object]]:
            raise RuntimeError("verified mismatch")

    monkeypatch.setattr(assurance_cli, "load_dotenv", lambda _path: None)
    monkeypatch.setattr(
        assurance_cli,
        "SourceRegistry",
        SimpleNamespace(load=lambda _sources, _budgets: registry),
    )
    monkeypatch.setattr(
        assurance_cli,
        "VerifiedQueryRegistry",
        SimpleNamespace(load=lambda _path, _known: Verified()),
    )
    monkeypatch.setattr(assurance_cli, "PostgresCatalog", _static_launch_catalog(catalog))
    monkeypatch.setattr(assurance_cli, "PostgresQueryExecutor", lambda: executor)
    monkeypatch.setattr(assurance_cli, "MetadataService", lambda *_args: object())
    monkeypatch.setattr(assurance_cli, "QueryService", lambda *_args: object())

    with pytest.raises(RuntimeError, match="verified mismatch"):
        await assurance_cli._run_verification(tmp_path)

    assert capsys.readouterr().out == ""
    assert events == ["executor", "catalog"]


def test_quality_and_verified_core_do_not_import_concrete_composition_adapters() -> None:
    forbidden = {
        "query_man.catalog.PostgresCatalog",
        "query_man.query.PostgresQueryExecutor",
        "query_man.registry.SourceRegistry",
    }

    for filename in ("quality.py", "verified.py"):
        path = ROOT_DIRECTORY / "src" / "query_man" / filename
        imported: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(
                    f"{node.module}.{alias.name}" for alias in node.names
                )

        assert forbidden.isdisjoint(imported), filename


def test_concrete_service_construction_stays_in_approved_composition_roots() -> None:
    source_root = ROOT_DIRECTORY / "src" / "query_man"
    allowed_files = {
        "SourceRegistry": {
            "app.py",
            "assurance_cli.py",
            "managed/runtime.py",
            "managed/source_admin.py",
        },
        "PostgresCatalog": {
            "app.py",
            "assurance_cli.py",
            "managed/runtime.py",
        },
        "PostgresQueryExecutor": {
            "app.py",
            "assurance_cli.py",
            "managed/runtime.py",
        },
        "MetadataService": {
            "app.py",
            "assurance_cli.py",
            "managed/runtime.py",
            "managed/source_admin.py",
        },
        "QueryService": {
            "app.py",
            "assurance_cli.py",
            "managed/runtime.py",
        },
    }
    if not (source_root / "managed").is_dir():
        for files in allowed_files.values():
            files.difference_update(
                path for path in files if path.startswith("managed/")
            )
    actual_files = {name: set() for name in allowed_files}

    for path in source_root.rglob("*.py"):
        relative_path = path.relative_to(source_root).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            if isinstance(called, ast.Name) and called.id in actual_files:
                actual_files[called.id].add(relative_path)
            elif (
                isinstance(called, ast.Attribute)
                and isinstance(called.value, ast.Name)
                and called.value.id == "SourceRegistry"
                and called.attr == "load"
            ):
                actual_files["SourceRegistry"].add(relative_path)

    assert actual_files == allowed_files


def _recording_close(events: list[object], value: object) -> Any:
    async def close() -> None:
        events.append(value)

    return close


def _static_launch_catalog(catalog: object) -> Any:
    def factory(*, reject_domain_columns: bool) -> object:
        assert reject_domain_columns is True
        return catalog

    return factory


def _patch_evaluation_composition(
    monkeypatch: pytest.MonkeyPatch,
    evaluation: object,
    events: list[object],
) -> None:
    registry = SimpleNamespace(list=lambda: [{"source_id": "known-source"}])
    verified = SimpleNamespace(revision_map=lambda: {})
    catalog = SimpleNamespace(close=_recording_close(events, "catalog"))
    monkeypatch.setattr(assurance_cli, "load_dotenv", lambda _path: None)
    monkeypatch.setattr(
        assurance_cli,
        "SourceRegistry",
        SimpleNamespace(load=lambda _sources, _budgets: registry),
    )
    monkeypatch.setattr(
        assurance_cli,
        "QualityEvaluation",
        SimpleNamespace(load=lambda _path, _known: evaluation),
    )
    monkeypatch.setattr(
        assurance_cli,
        "VerifiedQueryRegistry",
        SimpleNamespace(load=lambda _path, _known: verified),
    )
    monkeypatch.setattr(assurance_cli, "PostgresCatalog", _static_launch_catalog(catalog))
    monkeypatch.setattr(assurance_cli, "MetadataService", lambda *_args, **_kwargs: object())
