from __future__ import annotations

import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.generate_release_metadata import generate_metadata


def _release_files(directory: Path) -> None:
    wheel = directory / "pulse_coding_agent-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(
            "pulse_coding_agent-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: pulse-coding-agent\n"
            "Version: 0.1.0\n"
            "Requires-Dist: httpx==0.28.1\n"
            "Requires-Dist: keyring==25.7.0\n"
            "Requires-Dist: openai==2.44.0\n"
            "Requires-Dist: python-dotenv==1.2.2\n"
            "Requires-Dist: rich==15.0.0\n"
            "Requires-Dist: typer==0.26.8\n"
            "Requires-Dist: websockets==16.1\n",
        )
        archive.writestr(
            "pulse_coding_agent-0.1.0.dist-info/entry_points.txt",
            "[console_scripts]\npulse = pulse.cli:main\npulse-rpc = pulse.rpc:main\npulse-remote = pulse.sandbox.remote.server:main\n",
        )
    with tarfile.open(
        directory / "pulse_coding_agent-0.1.0.tar.gz", mode="w:gz"
    ) as archive:
        readme = directory / "README.md"
        readme.write_text("Pulse", encoding="utf-8")
        archive.add(readme, arcname="pulse_coding_agent-0.1.0/README.md")
        readme.unlink()


def test_release_metadata_binds_hashes_to_source_commit(tmp_path: Path) -> None:
    distributions = tmp_path / "dist"
    metadata = tmp_path / "metadata"
    distributions.mkdir()
    _release_files(distributions)

    manifest = generate_metadata(distributions, metadata, "a" * 40)

    assert manifest["source_commit"] == "a" * 40
    assert manifest["version"] == "0.1.0"
    assert len(manifest["artifacts"]) == 2
    saved = json.loads(
        (metadata / "release-manifest.json").read_text(encoding="utf-8")
    )
    assert saved == manifest
    checksums = (metadata / "SHA256SUMS").read_text(encoding="utf-8")
    assert "pulse_coding_agent-0.1.0-py3-none-any.whl" in checksums
    assert "pulse_coding_agent-0.1.0.tar.gz" in checksums


def test_release_metadata_rejects_abbreviated_commit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full 40-64"):
        generate_metadata(tmp_path, tmp_path / "metadata", "abc123")
