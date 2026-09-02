from __future__ import annotations

import ast
from fnmatch import fnmatchcase
from pathlib import Path

from tests.helpers import ROOT_DIRECTORY


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
        },
        "query_man.metadata.catalog.PostgresCatalog": {"runtime/composition.py"},
        "query_man.guarded_query.query.PostgresQueryExecutor": {"runtime/composition.py"},
        "query_man.metadata.service.MetadataService": {"runtime/composition.py"},
        "query_man.guarded_query.query.QueryService": {"runtime/composition.py"},
        "query_man.delivery.gateway.GatewayService": {"runtime/composition.py"},
        "query_man.delivery.authentication.OAuth2JWTBearerAuthenticator": {
            "runtime/composition.py"
        },
        "query_man.delivery.app.build_http_app": {"runtime/composition.py"},
        "query_man.runtime.diagnostic_capture.EncryptedDiagnosticCapture": {"runtime/*.py"},
        "query_man.runtime.diagnostic_capture.EncryptedDiagnosticCapture.from_base64": {
            "runtime/*.py"
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
                    violations.append(f"{relative_path}: imports source authority loader")
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Name, ast.Attribute)):
                    continue
                reference = _resolve_reference(node, bindings)
                if reference == source_registry or (
                    reference is not None and reference.startswith(f"{source_registry}.")
                ):
                    violations.append(f"{relative_path}:{node.lineno}: uses source authority loader")

    assert not violations, "Domain modules parse source authority:\n" + "\n".join(
        sorted(set(violations))
    )


def test_modules_do_not_import_cross_package_private_symbols_or_markers() -> None:
    source_root = ROOT_DIRECTORY / "src" / "query_man"
    marker_modules = {_module_name(path) for path in source_root.rglob("__init__.py")}
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
                violations.append(f"{relative_path}:{node.lineno}: uses private symbol {reference}")

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


def _source_trees(package: str | None = None) -> list[tuple[str, str, ast.Module]]:
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
                imported = f"{imported_module}.{alias.name}" if imported_module else alias.name
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
