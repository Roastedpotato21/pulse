from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def finalize_sbom(sbom_path: Path, manifest_path: Path) -> dict[str, Any]:
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if sbom.get("bomFormat") != "CycloneDX" or not isinstance(
        sbom.get("components"), list
    ):
        raise ValueError("SBOM is not a CycloneDX document with components")
    project = manifest.get("project")
    version = manifest.get("version")
    if not isinstance(project, str) or not isinstance(version, str):
        raise TypeError("release manifest has no project/version identity")

    project_ref = f"pkg:pypi/{project}@{version}"
    metadata = sbom.setdefault("metadata", {})
    metadata["component"] = {
        "bom-ref": project_ref,
        "name": project,
        "purl": project_ref,
        "type": "application",
        "version": version,
    }
    component_refs = sorted(
        {
            str(component["bom-ref"])
            for component in sbom["components"]
            if isinstance(component, dict) and component.get("bom-ref")
        }
    )
    dependencies = [
        item
        for item in sbom.get("dependencies", [])
        if isinstance(item, dict) and item.get("ref") != project_ref
    ]
    dependencies.append({"dependsOn": component_refs, "ref": project_ref})
    sbom["dependencies"] = dependencies
    sbom_path.write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return sbom


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bind a dependency CycloneDX SBOM to the released Pulse application."
    )
    parser.add_argument("sbom", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        sbom = finalize_sbom(args.sbom, args.manifest)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"SBOM finalization failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(f"Finalized CycloneDX SBOM with {len(sbom['components'])} components.")


if __name__ == "__main__":
    main()
