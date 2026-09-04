from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest
import yaml

from query_man.runtime.operator_shell import run_main
from tests.helpers import QUERY_CAVE_DIRECTORY, ROOT_DIRECTORY


def _copy_source_configuration(target_root: Path) -> None:
    target_config = target_root / "config"
    target_config.mkdir()
    shutil.copytree(QUERY_CAVE_DIRECTORY / "config" / "sources", target_config / "sources")
    shutil.copy2(
        QUERY_CAVE_DIRECTORY / "config" / "budget-profiles.yaml",
        target_config / "budget-profiles.yaml",
    )
    shutil.copy2(
        QUERY_CAVE_DIRECTORY / "config" / "database-profiles.yaml",
        target_config / "database-profiles.yaml",
    )


def test_console_script_targets_runtime_operator_shell() -> None:
    configuration = tomllib.loads(
        (ROOT_DIRECTORY / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert configuration["project"]["scripts"]["qm"] == (
        "query_man.runtime.operator_shell:main"
    )


def test_repository_root_validation_fails_closed_without_source_inventory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_main(["--root", str(ROOT_DIRECTORY), "source", "validate"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "Source package validation failed" in output.err
    assert "Traceback" not in output.err


def test_source_validate_succeeds_without_database_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_value = "database-password-must-not-appear"
    monkeypatch.setenv("UNRELATED_DATABASE_SECRET", private_value)

    assert run_main(["--root", str(QUERY_CAVE_DIRECTORY), "source", "validate"]) == 0

    output = capsys.readouterr()
    document = yaml.safe_load(output.out)
    expected_source_ids = sorted(
        path.name for path in (QUERY_CAVE_DIRECTORY / "config" / "sources").iterdir()
    )
    assert document == {
        "status": "valid",
        "source_directory": "config/sources",
        "budget_file": "config/budget-profiles.yaml",
        "database_file": "config/database-profiles.yaml",
        "source_count": len(expected_source_ids),
        "source_ids": expected_source_ids,
        "live_database_checked": False,
    }
    assert output.err == ""
    assert private_value not in output.out
    assert "query-man-source-validation-placeholder" not in output.out


def test_source_validate_resolves_local_environment_without_printing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _copy_source_configuration(tmp_path)
    database_file = tmp_path / "config" / "database-profiles.yaml"
    databases = yaml.safe_load(database_file.read_text(encoding="utf-8"))
    for profile in databases["profiles"].values():
        profile["host"] = "/invalid-without-env-resolution"
    database_file.write_text(
        yaml.safe_dump(databases, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    private_host = "private-database.internal"
    (tmp_path / ".env").write_text(
        f"QUERY_MAN_POSTGRES_HOST={private_host}\nQUERY_CAVE_POSTGRES_PORT=55432\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("QUERY_MAN_POSTGRES_HOST", raising=False)
    monkeypatch.delenv("QUERY_CAVE_POSTGRES_PORT", raising=False)

    assert run_main(["--root", str(tmp_path), "source", "validate"]) == 0

    output = capsys.readouterr()
    assert private_host not in output.out
    assert private_host not in output.err


def test_source_validate_failure_is_redacted_and_returns_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _copy_source_configuration(tmp_path)
    manifest_path = (
        tmp_path / "config" / "sources" / "query-cave" / "source.yaml"
    )
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    accidental_secret = "accidental-plaintext-credential"
    manifest["credential"] = accidental_secret
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    assert run_main(["--root", str(tmp_path), "source", "validate"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "Source package validation failed" in output.err
    assert accidental_secret not in output.err
    assert "Traceback" not in output.err


def test_source_validate_rejects_symlink_without_reading_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _copy_source_configuration(tmp_path)
    manifest_path = (
        tmp_path / "config" / "sources" / "query-cave" / "source.yaml"
    )
    accidental_secret = "must-not-read-symlink-target-secret"
    external_manifest = tmp_path / "external-source.yaml"
    external_manifest.write_text(accidental_secret, encoding="utf-8")
    manifest_path.unlink()
    manifest_path.symlink_to(external_manifest)

    assert run_main(["--root", str(tmp_path), "source", "validate"]) == 1

    output = capsys.readouterr()
    assert accidental_secret not in output.out + output.err
    assert "Traceback" not in output.err
