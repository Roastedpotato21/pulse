from __future__ import annotations

import argparse
import email.parser
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

FORBIDDEN_PARTS = {
    ".agent",
    ".env",
    ".git",
    ".pulse",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "gh",
    "node_modules",
    "vscode-extension",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".key",
    ".log",
    ".pem",
    ".pfx",
    ".p12",
    ".pyc",
    ".sqlite",
    ".sqlite3",
    ".vsix",
    ".wal",
    ".zip",
}
EXPECTED_ENTRY_POINTS = {"pulse", "pulse-remote", "pulse-rpc"}
EXPECTED_RUNTIME_REQUIREMENTS = {
    "httpx==0.28.1",
    "keyring==25.7.0",
    "openai==2.44.0",
    "prompt-toolkit==3.0.52",
    "python-dotenv==1.2.2",
    "rich==15.0.0",
    "typer==0.26.8",
    "websockets==16.1",
}


def _normalized_version(value: str) -> str:
    return value.removeprefix("v")


def _validate_names(archive: Path, names: list[str]) -> None:
    violations: list[str] = []
    for name in names:
        path = PurePosixPath(name.replace("\\", "/"))
        lowered_parts = {part.lower() for part in path.parts}
        if lowered_parts & FORBIDDEN_PARTS or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            violations.append(name)
    if violations:
        joined = "\n  ".join(sorted(violations))
        raise ValueError(f"{archive.name} contains forbidden release files:\n  {joined}")


def _metadata(wheel: Path):
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError(f"{wheel.name} must contain exactly one METADATA file")
        message = email.parser.BytesParser().parsebytes(archive.read(metadata_names[0]))
    return message


def _metadata_version(wheel: Path) -> str:
    version = _metadata(wheel).get("Version")
    if not version:
        raise ValueError(f"{wheel.name} metadata has no Version")
    return version


def _validate_runtime_requirements(wheel: Path) -> None:
    requirements = set(_metadata(wheel).get_all("Requires-Dist", []))
    if requirements != EXPECTED_RUNTIME_REQUIREMENTS:
        missing = sorted(EXPECTED_RUNTIME_REQUIREMENTS - requirements)
        unexpected = sorted(requirements - EXPECTED_RUNTIME_REQUIREMENTS)
        raise ValueError(
            f"{wheel.name} runtime dependency pins differ from the reviewed set; "
            f"missing={missing}, unexpected={unexpected}"
        )


def _entry_points(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/entry_points.txt")
        ]
        if len(names) != 1:
            raise ValueError(f"{wheel.name} must contain exactly one entry_points.txt")
        entries = archive.read(names[0]).decode("utf-8")
    return {
        line.split("=", 1)[0].strip()
        for line in entries.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


def verify(directory: Path, expected_version: str | None) -> None:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    expected_files = {*wheels, *sdists}
    unexpected = sorted(
        path.name
        for path in directory.iterdir()
        if path.is_file() and path not in expected_files and path.name != ".gitignore"
    )
    if len(wheels) != 1 or len(sdists) != 1 or unexpected:
        raise ValueError(
            "release directory must contain exactly one wheel and one source distribution; "
            f"found wheels={len(wheels)}, sdists={len(sdists)}, unexpected={unexpected}"
        )

    wheel = wheels[0]
    sdist = sdists[0]
    with zipfile.ZipFile(wheel) as archive:
        _validate_names(wheel, archive.namelist())
    with tarfile.open(sdist, mode="r:gz") as archive:
        _validate_names(sdist, archive.getnames())

    version = _metadata_version(wheel)
    _validate_runtime_requirements(wheel)
    if expected_version and version != _normalized_version(expected_version):
        raise ValueError(
            f"artifact version {version!r} does not match release {expected_version!r}"
        )

    missing_entries = EXPECTED_ENTRY_POINTS - _entry_points(wheel)
    if missing_entries:
        raise ValueError(f"wheel is missing entry points: {sorted(missing_entries)}")

    print(f"Verified {wheel.name} and {sdist.name} (version {version}).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Pulse release artifact contents.")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    try:
        verify(args.directory, args.expected_version)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"Release artifact verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
