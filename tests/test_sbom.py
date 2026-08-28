from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.finalize_sbom import finalize_sbom


def test_finalize_sbom_adds_application_root_and_dependency_edges(
    tmp_path: Path,
) -> None:
    sbom_path = tmp_path / "sbom.json"
    manifest_path = tmp_path / "manifest.json"
    sbom_path.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "components": [
                    {"bom-ref": "dependency-b", "name": "b", "type": "library"},
                    {"bom-ref": "dependency-a", "name": "a", "type": "library"},
                ],
                "dependencies": [{"ref": "dependency-a"}],
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps({"project": "pulse-coding-agent", "version": "0.1.0"}),
        encoding="utf-8",
    )

    sbom = finalize_sbom(sbom_path, manifest_path)

    root = sbom["metadata"]["component"]
    assert root["purl"] == "pkg:pypi/pulse-coding-agent@0.1.0"
    root_edges = next(
        item for item in sbom["dependencies"] if item["ref"] == root["bom-ref"]
    )
    assert root_edges["dependsOn"] == ["dependency-a", "dependency-b"]


def test_finalize_sbom_rejects_non_cyclonedx_input(tmp_path: Path) -> None:
    sbom_path = tmp_path / "sbom.json"
    manifest_path = tmp_path / "manifest.json"
    sbom_path.write_text('{"components": []}', encoding="utf-8")
    manifest_path.write_text(
        '{"project": "pulse-coding-agent", "version": "0.1.0"}', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="CycloneDX"):
        finalize_sbom(sbom_path, manifest_path)
