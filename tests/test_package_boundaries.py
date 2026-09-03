from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import ROOT_DIRECTORY


def _module_name(package_root: Path, path: Path) -> str:
    relative = path.parent.relative_to(package_root)
    return ".".join(("query_man", *relative.parts))


def _package_markers() -> tuple[str, ...]:
    package_root = ROOT_DIRECTORY / "src" / "query_man"
    return tuple(
        _module_name(package_root, path)
        for path in sorted(package_root.glob("*/__init__.py"))
    )


@pytest.mark.parametrize("module", _package_markers())
def test_package_markers_do_not_eager_load_implementation(module: str) -> None:
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
