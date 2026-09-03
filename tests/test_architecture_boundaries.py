from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers import ROOT_DIRECTORY


def test_delivery_does_not_import_database_drivers() -> None:
    violations: list[str] = []

    for relative_path, importer, tree in _source_trees("delivery"):
        for imported in _imported_names(tree, importer):
            if imported == "psycopg" or imported.startswith(
                ("psycopg.", "psycopg_pool")
            ):
                violations.append(f"{relative_path}: imports {imported}")

    assert not violations, "Delivery acquired database access:\n" + "\n".join(
        violations
    )


def test_database_adapters_are_created_only_by_runtime_composition() -> None:
    adapters = {
        "query_man.metadata.catalog.PostgresCatalog",
        "query_man.guarded_query.query.PostgresQueryExecutor",
    }
    violations: list[str] = []

    for relative_path, importer, tree in _source_trees():
        if relative_path == "runtime/composition.py":
            continue
        bindings = _import_bindings(tree, importer)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            reference = _resolve_reference(node.func, bindings)
            if reference in adapters:
                violations.append(f"{relative_path}:{node.lineno}: creates {reference}")

    assert not violations, "Database adapter escaped composition root:\n" + "\n".join(
        violations
    )


def test_domain_modules_do_not_parse_source_authority() -> None:
    violations: list[str] = []

    for package in ("metadata", "guarded_query"):
        for relative_path, importer, tree in _source_trees(package):
            bindings = _import_bindings(tree, importer)
            for imported in _imported_names(tree, importer):
                if imported == "yaml" or imported.startswith("yaml."):
                    violations.append(f"{relative_path}: imports {imported}")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                reference = _resolve_reference(node.func, bindings)
                if reference == "query_man.source_catalog.registry.SourceRegistry.load":
                    violations.append(
                        f"{relative_path}:{node.lineno}: loads source authority"
                    )

    assert not violations, "Domain module parsed source authority:\n" + "\n".join(
        violations
    )


def test_modules_do_not_import_cross_package_private_symbols() -> None:
    violations: list[str] = []

    for relative_path, importer, tree in _source_trees():
        importer_package = _logical_package(importer)
        bindings = _import_bindings(tree, importer)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if (
                        alias.name.startswith("query_man.")
                        and _logical_package(alias.name) != importer_package
                        and _has_private_segment(alias.name)
                    ):
                        violations.append(
                            f"{relative_path}:{node.lineno}: imports private module "
                            f"{alias.name}"
                        )
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            imported_module = _absolute_import_module(node, importer)
            if (
                not imported_module.startswith("query_man.")
                or _logical_package(imported_module) == importer_package
            ):
                continue
            for alias in node.names:
                if alias.name.startswith("_") or _has_private_segment(imported_module):
                    violations.append(
                        f"{relative_path}:{node.lineno}: imports private symbol "
                        f"{imported_module}.{alias.name}"
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
                violations.append(f"{relative_path}:{node.lineno}: uses {reference}")

    assert not violations, "Private symbol crossed package boundary:\n" + "\n".join(
        violations
    )


def _source_trees(package: str | None = None) -> list[tuple[str, str, ast.Module]]:
    source_root = ROOT_DIRECTORY / "src" / "query_man"
    search_root = source_root / package if package is not None else source_root
    trees: list[tuple[str, str, ast.Module]] = []
    for path in sorted(search_root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        trees.append(
            (
                path.relative_to(source_root).as_posix(),
                _module_name(path),
                ast.parse(path.read_text(encoding="utf-8")),
            )
        )
    return trees


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT_DIRECTORY / "src").with_suffix("")
    return ".".join(relative.parts)


def _imported_names(tree: ast.Module, importer: str) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_import_module(node, importer)
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    return imported


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
                bindings[alias.asname or alias.name] = f"{imported_module}.{alias.name}"
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
    return ".".join((bindings.get(parts[0], parts[0]), *parts[1:]))


def _absolute_import_module(node: ast.ImportFrom, importer: str) -> str:
    if node.level == 0:
        return node.module or ""
    importer_package = importer.split(".")[:-1]
    base = importer_package[: len(importer_package) - (node.level - 1)]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _logical_package(module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if len(parts) > 2 and parts[0] == "query_man" else None


def _has_private_segment(module: str) -> bool:
    return any(part.startswith("_") for part in module.split(".")[1:])
