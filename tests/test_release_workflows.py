from __future__ import annotations

import re
from pathlib import Path


def test_release_actions_are_immutable_and_attestations_are_authorized() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    action_refs = re.findall(r"uses:\s+[^@\s]+@([^\s]+)", workflow)
    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
    assert "subject-checksums: release-metadata/SHA256SUMS" in workflow
    assert "sbom-path: release-metadata/sbom.cdx.json" in workflow
    assert 'tags: ["v*"]' in workflow
    assert "scripts/run_provider_e2e.py" in workflow
    assert "scripts/run_docker_release_tests.py" in workflow
    assert "release-metadata/docker-release.xml" in workflow
    assert "docker-full.xml" not in workflow
    assert "path: release-metadata/*.xml" in workflow
    assert "if-no-files-found: warn" in workflow
    assert "needs: [build-and-verify, provider-e2e, docker-security]" in workflow
    assert 'gh release create "$RELEASE_TAG"' in workflow

    ci_workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "scripts/run_docker_release_tests.py" in ci_workflow
    assert "release-metadata/docker-release.xml" in ci_workflow
    assert "docker-full.xml" not in ci_workflow
    assert "-p no:unraisableexception" in ci_workflow
    assert "path: release-metadata/*.xml" in ci_workflow
    assert "if-no-files-found: warn" in ci_workflow
    assert "needs: [test, dependency-audit, secret-scan, vscode-extension, docker-security]" in ci_workflow
    assert "cancel-in-progress: false" in ci_workflow
    assert 'uv sync --locked --python "${{ matrix.python }}"' in ci_workflow
    assert "uv run python --version" in ci_workflow
    assert "runs-on: ubuntu-24.04" in ci_workflow
    assert "docker run --rm python:3.11-slim python --version" in ci_workflow
    assert "sudo journalctl -u docker --no-pager -n 100" in ci_workflow
