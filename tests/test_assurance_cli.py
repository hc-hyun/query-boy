from __future__ import annotations

import ast
import io
import logging
import sys
import tomllib
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

import query_man.assurance.cli as assurance_cli
from query_man.assurance.quality import QualityGateError, QualityReport
from query_man.source_catalog.reader_policy import ReaderSessionPolicyError
from tests.helpers import ROOT_DIRECTORY


class _UnrenderableLogValue:
    def __str__(self) -> str:
        raise AssertionError("database dependency log content must not be rendered")


def test_console_scripts_target_the_assurance_cli_composition_root() -> None:
    configuration = tomllib.loads(
        (ROOT_DIRECTORY / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert configuration["project"]["scripts"]["query-man-evaluate"] == (
        "query_man.assurance.cli:evaluate_main"
    )
    assert configuration["project"]["scripts"]["query-man-verify"] == (
        "query_man.assurance.cli:verify_main"
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


@pytest.mark.parametrize(
    ("command", "entrypoint", "runner_name", "logger_name", "database_error", "suppress"),
    (
        (
            "query-man-evaluate",
            assurance_cli.evaluate_main,
            "_run_evaluation",
            "psycopg.pool",
            OperationalError(
                "connection failed host=postgres.internal user=private_reader"
            ),
            False,
        ),
        (
            "query-man-verify",
            assurance_cli.verify_main,
            "_run_verification",
            "psycopg_pool.worker",
            PoolTimeout("pool timed out for private-db.internal:5432"),
            False,
        ),
        (
            "query-man-evaluate",
            assurance_cli.evaluate_main,
            "_run_evaluation",
            "psycopg.pool",
            ReaderSessionPolicyError("reader policy exposed private-db"),
            True,
        ),
    ),
)
def test_cli_maps_only_database_dependency_chains_and_never_renders_driver_logs(
    command: str,
    entrypoint: Any,
    runner_name: str,
    logger_name: str,
    database_error: Exception,
    suppress: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(_root: Path) -> None:
        try:
            raise database_error
        except Exception:
            logging.getLogger(logger_name).warning(
                "driver failure endpoint=%s detail=%s",
                "private-db.internal:5432",
                _UnrenderableLogValue(),
                exc_info=True,
            )
            if suppress:
                raise RuntimeError("database wrapper") from None
            raise RuntimeError("database wrapper") from database_error

    parent_logger = logging.getLogger(logger_name.split(".", 1)[0])
    child_logger = logging.getLogger(logger_name)
    root_logger = logging.getLogger()
    original_factory = logging.getLogRecordFactory()
    last_resort_stream = io.StringIO()
    last_resort = logging.StreamHandler(last_resort_stream)
    last_resort.setLevel(logging.WARNING)
    monkeypatch.setattr(logging, "lastResort", last_resort)
    monkeypatch.setattr(root_logger, "handlers", [])
    monkeypatch.setattr(parent_logger, "handlers", [])
    monkeypatch.setattr(parent_logger, "level", logging.ERROR)
    monkeypatch.setattr(parent_logger, "propagate", True)
    monkeypatch.setattr(parent_logger, "disabled", False)
    monkeypatch.setattr(child_logger, "handlers", [])
    monkeypatch.setattr(child_logger, "level", logging.NOTSET)
    monkeypatch.setattr(child_logger, "propagate", True)
    monkeypatch.setattr(child_logger, "disabled", False)
    monkeypatch.setattr(assurance_cli, runner_name, fail)
    monkeypatch.setattr(sys, "argv", [command, "--root", str(tmp_path)])

    with pytest.raises(SystemExit) as captured:
        entrypoint()

    streams = capsys.readouterr()
    assert captured.value.code == 1
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True
    assert streams.out == ""
    assert streams.err == (
        '{"event": "database_dependency_log"}\n'
        '{"error_code": "DATABASE_UNAVAILABLE", "status": "failed"}\n'
    )
    assert "private-db" not in streams.err
    assert "driver failure" not in streams.err
    assert last_resort_stream.getvalue() == ""
    assert parent_logger.handlers == []
    assert parent_logger.level == logging.ERROR
    assert parent_logger.propagate is True
    assert parent_logger.disabled is False
    assert logging.getLogRecordFactory() is original_factory


def test_cli_preserves_non_database_failure_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(_root: Path) -> None:
        raise RuntimeError("verified mismatch")

    monkeypatch.setattr(assurance_cli, "_run_verification", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        ["query-man-verify", "--root", str(tmp_path)],
    )

    with pytest.raises(RuntimeError, match="verified mismatch"):
        assurance_cli.verify_main()

    streams = capsys.readouterr()
    assert streams.out == ""
    assert streams.err == ""


@pytest.mark.asyncio
async def test_evaluation_cli_preserves_bootstrap_paths_output_exit_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[object] = []
    registry = SimpleNamespace(source_ids=lambda: frozenset({"known-source"}))
    catalog = SimpleNamespace(close=_recording_close(events, "catalog"))
    report = QualityReport(1, 1.0, 1.0, 1_024, 1_024, ())

    class Evaluation:
        async def evaluate(self, metadata: object) -> QualityReport:
            events.append(("evaluate", metadata))
            return report

    class Verified:
        def revision_map(self) -> dict[str, frozenset[str]]:
            return {"known-source": frozenset({"sha256:revision"})}

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
    registry = SimpleNamespace(source_ids=lambda: frozenset({"known-source"}))
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
    registry = SimpleNamespace(source_ids=lambda: frozenset({"known-source"}))
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


def test_architecture_reference_resolution_handles_relative_and_aliased_imports() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "from ..metadata.catalog import PostgresCatalog as Catalog",
                "import query_man.metadata.catalog as catalog_module",
                "Catalog()",
                "catalog_module._CatalogValidationError",
            )
        )
    )
    bindings = _import_bindings(tree, "query_man.runtime.example")
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    private_attributes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.startswith("_")
    ]

    assert [_resolve_reference(node.func, bindings) for node in calls] == [
        "query_man.metadata.catalog.PostgresCatalog"
    ]
    assert [_resolve_reference(node, bindings) for node in private_attributes] == [
        "query_man.metadata.catalog._CatalogValidationError"
    ]


def test_concrete_capabilities_are_created_only_in_approved_composition_roots() -> None:
    allowed_callers = {
        "query_man.source_catalog.registry.SourceRegistry.load": {
            "runtime/composition.py",
            "runtime/operator_backend.py",
            "assurance/cli.py",
        },
        "query_man.metadata.catalog.PostgresCatalog": {
            "runtime/composition.py",
            "assurance/cli.py",
        },
        "query_man.guarded_query.query.PostgresQueryExecutor": {
            "runtime/composition.py",
            "assurance/cli.py",
        },
        "query_man.metadata.service.MetadataService": {
            "runtime/composition.py",
            "assurance/cli.py",
        },
        "query_man.guarded_query.query.QueryService": {
            "runtime/composition.py",
            "assurance/cli.py",
        },
        "query_man.delivery.gateway.GatewayService": {
            "runtime/composition.py",
        },
        "query_man.delivery.authentication.OAuth2JWTBearerAuthenticator": {
            "runtime/composition.py",
        },
        "query_man.delivery.app.build_http_app": {
            "runtime/composition.py",
        },
        "query_man.runtime.diagnostic_capture.EncryptedDiagnosticCapture": {
            "runtime/*.py",
        },
        "query_man.runtime.diagnostic_capture.EncryptedDiagnosticCapture.from_base64": {
            "runtime/*.py",
        },
    }
    violations: list[str] = []

    for relative_path, importer, tree in _source_trees():
        bindings = _import_bindings(tree, importer)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            capability = _resolve_reference(node.func, bindings)
            if capability is None or capability not in allowed_callers:
                continue
            if not any(
                fnmatchcase(relative_path, pattern)
                for pattern in allowed_callers[capability]
            ):
                violations.append(f"{relative_path}:{node.lineno}: {capability}")

    assert not violations, "Concrete composition escaped its approved roots:\n" + "\n".join(
        violations
    )


def test_delivery_does_not_depend_on_database_adapters() -> None:
    forbidden_references = {
        "query_man.metadata.catalog.PostgresCatalog",
        "query_man.guarded_query.query.PostgresQueryExecutor",
        "query_man.source_catalog.registry.SourceRegistry",
    }
    violations: list[str] = []

    for relative_path, importer, tree in _source_trees("delivery"):
        bindings = _import_bindings(tree, importer)
        for imported in bindings.values():
            if imported == "psycopg" or imported.startswith(("psycopg.", "psycopg_pool")):
                violations.append(f"{relative_path}: imports {imported}")
            if imported in forbidden_references:
                violations.append(f"{relative_path}: imports {imported}")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Name, ast.Attribute)):
                continue
            reference = _resolve_reference(node, bindings)
            if reference is None:
                continue
            for forbidden in forbidden_references:
                if reference == forbidden or reference.startswith(f"{forbidden}."):
                    violations.append(f"{relative_path}:{node.lineno}: uses {forbidden}")

    assert not violations, "Delivery acquired database capabilities:\n" + "\n".join(
        sorted(set(violations))
    )


def test_domain_modules_do_not_parse_source_authority() -> None:
    source_registry = "query_man.source_catalog.registry.SourceRegistry"
    violations: list[str] = []

    for package in ("metadata", "guarded_query"):
        for relative_path, importer, tree in _source_trees(package):
            bindings = _import_bindings(tree, importer)
            for imported in bindings.values():
                if imported == "yaml" or imported.startswith("yaml."):
                    violations.append(f"{relative_path}: imports {imported}")
                if imported == source_registry:
                    violations.append(
                        f"{relative_path}: imports source authority loader"
                    )
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Name, ast.Attribute)):
                    continue
                reference = _resolve_reference(node, bindings)
                if reference == source_registry or (
                    reference is not None
                    and reference.startswith(f"{source_registry}.")
                ):
                    violations.append(
                        f"{relative_path}:{node.lineno}: uses source authority loader"
                    )

    assert not violations, "Domain modules parse source authority:\n" + "\n".join(
        sorted(set(violations))
    )


def test_modules_do_not_import_cross_package_private_symbols_or_markers() -> None:
    source_root = ROOT_DIRECTORY / "src" / "query_man"
    marker_modules = {
        _module_name(path)
        for path in source_root.rglob("__init__.py")
    }
    violations: list[str] = []

    for relative_path, importer, tree in _source_trees():
        importer_package = _logical_package(importer)
        bindings = _import_bindings(tree, importer)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in marker_modules:
                        violations.append(
                            f"{relative_path}:{node.lineno}: imports package marker {alias.name}"
                        )
                    imported_package = _logical_package(alias.name)
                    if (
                        alias.name.startswith("query_man.")
                        and imported_package != importer_package
                        and _has_private_segment(alias.name)
                    ):
                        violations.append(
                            f"{relative_path}:{node.lineno}: imports private module {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                imported_module = _absolute_import_module(node, importer)
                if imported_module in marker_modules:
                    violations.append(
                        f"{relative_path}:{node.lineno}: imports through package marker "
                        f"{imported_module}"
                    )
                imported_package = _logical_package(imported_module)
                if (
                    not imported_module.startswith("query_man.")
                    or imported_package == importer_package
                ):
                    continue
                for alias in node.names:
                    imported = f"{imported_module}.{alias.name}"
                    if alias.name.startswith("_") or _has_private_segment(imported_module):
                        violations.append(
                            f"{relative_path}:{node.lineno}: imports private symbol {imported}"
                        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
                continue
            reference = _resolve_reference(node, bindings)
            if (
                reference is not None
                and reference.startswith("query_man.")
                and _logical_package(reference) != importer_package
            ):
                violations.append(
                    f"{relative_path}:{node.lineno}: uses private symbol {reference}"
                )

    assert not violations, "Private/package-marker imports crossed boundaries:\n" + "\n".join(
        violations
    )


def test_leaf_module_import_graph_is_acyclic() -> None:
    trees = _source_trees()
    leaf_modules = {module for _path, module, _tree in trees}
    graph = {module: set() for module in leaf_modules}

    for _relative_path, importer, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in leaf_modules:
                        graph[importer].add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                imported_module = _absolute_import_module(node, importer)
                if imported_module in leaf_modules:
                    graph[importer].add(imported_module)
                    continue
                for alias in node.names:
                    imported_leaf = f"{imported_module}.{alias.name}"
                    if imported_leaf in leaf_modules:
                        graph[importer].add(imported_leaf)

    cycle = _find_cycle(graph)
    assert cycle is None, "Leaf-module import cycle: " + " -> ".join(cycle or ())


def _source_trees(
    package: str | None = None,
) -> list[tuple[str, str, ast.Module]]:
    source_root = ROOT_DIRECTORY / "src" / "query_man"
    search_root = source_root / package if package is not None else source_root
    trees: list[tuple[str, str, ast.Module]] = []
    for path in sorted(search_root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        relative_path = path.relative_to(source_root).as_posix()
        trees.append(
            (
                relative_path,
                _module_name(path),
                ast.parse(path.read_text(encoding="utf-8")),
            )
        )
    return trees


def _module_name(path: Path) -> str:
    source_root = ROOT_DIRECTORY / "src"
    relative = path.relative_to(source_root).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join(parts)


def _import_bindings(tree: ast.Module, importer: str) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".", 1)[0]
                bindings[local_name] = alias.name if alias.asname else local_name
        elif isinstance(node, ast.ImportFrom):
            imported_module = _absolute_import_module(node, importer)
            for alias in node.names:
                imported = (
                    f"{imported_module}.{alias.name}"
                    if imported_module
                    else alias.name
                )
                bindings[alias.asname or alias.name] = imported
    return bindings


def _resolve_reference(node: ast.expr, bindings: dict[str, str]) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    parts.reverse()
    root = bindings.get(parts[0], parts[0])
    return ".".join((root, *parts[1:]))


def _absolute_import_module(node: ast.ImportFrom, importer: str) -> str:
    if node.level == 0:
        return node.module or ""
    importer_package = importer.split(".")[:-1]
    parent_count = node.level - 1
    base = importer_package[: len(importer_package) - parent_count]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _logical_package(module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if len(parts) > 2 and parts[0] == "query_man" else None


def _has_private_segment(module: str) -> bool:
    return any(part.startswith("_") for part in module.split(".")[1:])


def _find_cycle(graph: dict[str, set[str]]) -> tuple[str, ...] | None:
    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []

    def visit(module: str) -> tuple[str, ...] | None:
        if module in visiting:
            start = path.index(module)
            return (*path[start:], module)
        if module in visited:
            return None
        visiting.add(module)
        path.append(module)
        for dependency in sorted(graph[module]):
            cycle = visit(dependency)
            if cycle is not None:
                return cycle
        path.pop()
        visiting.remove(module)
        visited.add(module)
        return None

    for module in sorted(graph):
        cycle = visit(module)
        if cycle is not None:
            return cycle
    return None


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
    registry = SimpleNamespace(source_ids=lambda: frozenset({"known-source"}))
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
