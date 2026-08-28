from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

if __package__:
    from scripts.verify_release_artifacts import _metadata_version, verify
else:
    from verify_release_artifacts import _metadata_version, verify

_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_metadata(
    distribution_dir: Path,
    output_dir: Path,
    source_commit: str,
) -> dict[str, object]:
    if not _COMMIT_PATTERN.fullmatch(source_commit):
        raise ValueError("source commit must be a full 40-64 character hexadecimal hash")

    verify(distribution_dir, expected_version=None)
    wheel = next(distribution_dir.glob("*.whl"))
    artifacts = sorted(
        (
            {
                "name": path.name,
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
            for path in distribution_dir.iterdir()
            if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
        ),
        key=lambda item: str(item["name"]),
    )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "project": "pulse-coding-agent",
        "version": _metadata_version(wheel),
        "source_commit": source_commit.lower(),
        "artifacts": artifacts,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "SHA256SUMS").write_text(
        "".join(f'{item["sha256"]}  {item["name"]}\n' for item in artifacts),
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate checksums and a source-bound Pulse release manifest."
    )
    parser.add_argument("distribution_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    try:
        manifest = generate_metadata(
            args.distribution_dir, args.output_dir, args.source_commit
        )
    except (OSError, ValueError) as error:
        print(f"Release metadata generation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(
        f"Generated metadata for {len(manifest['artifacts'])} release artifacts."
    )


if __name__ == "__main__":
    main()
