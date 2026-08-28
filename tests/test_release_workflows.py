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
