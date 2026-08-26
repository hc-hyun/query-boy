from __future__ import annotations

import ast
import subprocess
import sys
from graphlib import TopologicalSorter
from pathlib import Path

import pytest

from tests.helpers import ROOT_DIRECTORY


def _module_name(source_root: Path, path: Path) -> str:
    parts = list(path.relative_to(source_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(("query_man", *parts))


def test_root_package_keeps_only_shared_transition_files() -> None:
    source_root = ROOT_DIRECTORY / "src" / "query_man"

    assert {path.name for path in source_root.glob("*.py")} == {
        "__init__.py",
        "errors.py",
    }


@pytest.mark.parametrize(
    "package",
    (
        "assurance",
        "delivery",
        "guarded_query",
        "managed",
        "metadata",
        "runtime",
        "source_catalog",
    ),
)
def test_package_markers_do_not_eager_load_siblings(package: str) -> None:
    module = f"query_man.{package}"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib, sys; "
                f"importlib.import_module({module!r}); "
                "loaded = {name for name in sys.modules if name == 'query_man' "
                "or name.startswith('query_man.')}; "
                f"assert loaded == {{'query_man', {module!r}}}, sorted(loaded)"
            ),
        ],
        check=False,
        capture_output=True,
        cwd=ROOT_DIRECTORY,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_static_runtime_composition_does_not_load_managed_package() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import query_man.runtime.composition; "
                "assert not any(name == 'query_man.managed' or "
                "name.startswith('query_man.managed.') for name in sys.modules)"
            ),
        ],
        check=False,
        capture_output=True,
        cwd=ROOT_DIRECTORY,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_source_module_import_graph_is_acyclic() -> None:
    source_root = ROOT_DIRECTORY / "src" / "query_man"
    paths = sorted(source_root.rglob("*.py"))
    modules = {_module_name(source_root, path) for path in paths}
    graph: dict[str, set[str]] = {module: set() for module in modules}

    for path in paths:
        module = _module_name(source_root, path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in modules:
                graph[module].add(node.module)
            elif isinstance(node, ast.Import):
                graph[module].update(
                    alias.name for alias in node.names if alias.name in modules
                )

    tuple(TopologicalSorter(graph).static_order())


def test_non_managed_modules_only_lazy_import_managed_runtime_dispatch() -> None:
    source_root = ROOT_DIRECTORY / "src" / "query_man"
    managed_imports: list[tuple[str, str, tuple[str, ...], bool]] = []

    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root)
        if relative.parts[0] == "managed":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if not (
                node.module == "query_man.managed"
                or node.module.startswith("query_man.managed.")
            ):
                continue
            ancestor: ast.AST = node
            guarded = False
            while ancestor in parents:
                ancestor = parents[ancestor]
                guarded = guarded or isinstance(ancestor, ast.If)
            managed_imports.append(
                (
                    relative.as_posix(),
                    node.module,
                    tuple(alias.name for alias in node.names),
                    guarded,
                )
            )

    assert managed_imports == [
        (
            "runtime/server.py",
            "query_man.managed.runtime",
            ("build_app",),
            True,
        )
    ]
