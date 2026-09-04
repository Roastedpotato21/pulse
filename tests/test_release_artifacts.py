from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts.verify_release_artifacts import (
    _normalized_version,
    _validate_content,
    _validate_names,
    _validate_public_oauth_config,
    _validate_runtime_requirements,
)


def test_release_tag_version_normalization() -> None:
    assert _normalized_version("v1.2.3") == "1.2.3"
    assert _normalized_version("1.2.3") == "1.2.3"


def test_readme_uses_the_distribution_name_for_pip_installation() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "python -m pip install pulse-coding-agent==0.1.1" in readme
    assert "Do not run `pip install pulse`" in readme


def test_repository_metadata_has_no_placeholder_organization() -> None:
    package_manifest = Path("vscode-extension/package.json").read_text(encoding="utf-8")
    extension_test = Path("vscode-extension/src/test/extension.test.ts").read_text(
        encoding="utf-8"
    )

    assert "your-org" not in package_manifest
    assert "your-org" not in extension_test


@pytest.mark.parametrize(
    "name",
    [
        "pulse/.env",
        "pulse/.agent/session.json",
        "pulse/cache.sqlite3",
        "pulse/private.pem",
        "pulse/debug.log",
        "pulse/bundle.vsix",
    ],
)
def test_release_artifact_rejects_private_or_generated_files(name: str) -> None:
    with pytest.raises(ValueError, match="forbidden release files"):
        _validate_names(Path("artifact.whl"), [name])


def test_release_artifact_accepts_package_files() -> None:
    _validate_names(
        Path("artifact.whl"),
        ["pulse/__init__.py", "pulse/py.typed", "pulse/sandbox/SECURITY.md"],
    )


@pytest.mark.parametrize("name", ["../outside.py", "/absolute.py", "pkg/../../outside.py"])
def test_release_artifact_rejects_path_traversal(name: str) -> None:
    with pytest.raises(ValueError, match="forbidden release files"):
        _validate_names(Path("artifact.whl"), [name])


def test_invalid_wheel_is_rejected(tmp_path: Path) -> None:
    wheel = tmp_path / "invalid.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr("pulse/.env", "SECRET=value")

    with (
        zipfile.ZipFile(wheel) as archive,
        pytest.raises(ValueError, match=r"pulse/.env"),
    ):
        _validate_names(wheel, archive.namelist())


def test_release_artifact_rejects_unreviewed_runtime_dependencies(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "invalid.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(
            "pulse_coding_agent-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: pulse-coding-agent\n"
            "Version: 0.1.0\n"
            "Requires-Dist: openai>=2\n",
        )

    with pytest.raises(ValueError, match="runtime dependency pins"):
        _validate_runtime_requirements(wheel)


def test_release_artifact_requires_configured_public_oauth_client(tmp_path: Path) -> None:
    wheel = tmp_path / "invalid.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(
            "pulse/_product_oauth.json",
            '{"client_id":"not-configured","redirect_uri":"http://127.0.0.1"}',
        )

    with pytest.raises(ValueError, match="placeholder Google OAuth client ID"):
        _validate_public_oauth_config(wheel)


def test_release_artifact_rejects_oauth_secret_field(tmp_path: Path) -> None:
    wheel = tmp_path / "invalid.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(
            "pulse/_product_oauth.json",
            '{"client_id":"123456789012-release.apps.googleusercontent.com",'
            '"client_secret":"must-not-ship","redirect_uri":"http://127.0.0.1"}',
        )

    with pytest.raises(ValueError, match="configuration fields"):
        _validate_public_oauth_config(wheel)


def test_release_artifact_rejects_internal_home_paths() -> None:
    with pytest.raises(ValueError, match="internal paths"):
        _validate_content(
            Path("artifact.whl"),
            [("report.txt", b"C:\\Users\\developer\\private-project")],
        )


def test_release_artifact_rejects_high_confidence_secret_material() -> None:
    with pytest.raises(ValueError, match="secret material"):
        _validate_content(
            Path("artifact.whl"),
            [("module.py", b"token = 'GOCSPX-abcdefghijklmnopqrstuvwxyz123456'")],
        )
