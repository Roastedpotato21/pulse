from __future__ import annotations

import argparse
import email.parser
import json
import re
import stat
import sys
import tarfile
import urllib.parse
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
    "rich==15.0.0",
    "typer==0.26.8",
    "websockets==16.1",
}
ALLOWED_SDIST_ROOT_FILES = {
    ".gitignore",
    "CHANGELOG.md",
    "hatch_build.py",
    "PKG-INFO",
    "PRIVACY.md",
    "README.md",
    "SECURITY.md",
    "pyproject.toml",
}
GOOGLE_CLIENT_ID_PATTERN = re.compile(
    r"^[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com$"
)
INTERNAL_PATH_PATTERNS = (
    re.compile(rb"(?i)[A-Z]:\\Users\\[^\\\s]+"),
    re.compile(rb"(?i)/(?:home|Users)/[^/\s]+"),
)
PRIVATE_KEY_MARKER = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
GOOGLE_CLIENT_SECRET_PATTERN = re.compile(rb"GOCSPX-[0-9A-Za-z_-]{20,}")
HIGH_CONFIDENCE_SECRET_PATTERNS = (
    re.compile(rb"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"gh[pousr]_[0-9A-Za-z]{30,}"),
    re.compile(rb"sk-(?:proj-)?[0-9A-Za-z_-]{24,}"),
)
MAX_RELEASE_MEMBER_BYTES = 10 * 1024 * 1024
MAX_RELEASE_UNCOMPRESSED_BYTES = 50 * 1024 * 1024


def _normalized_version(value: str) -> str:
    return value.removeprefix("v")


def _validate_names(archive: Path, names: list[str]) -> None:
    violations: list[str] = []
    for name in names:
        path = PurePosixPath(name.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            violations.append(name)
            continue
        lowered_parts = {part.lower() for part in path.parts}
        if lowered_parts & FORBIDDEN_PARTS or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            violations.append(name)
    if violations:
        joined = "\n  ".join(sorted(violations))
        raise ValueError(f"{archive.name} contains forbidden release files:\n  {joined}")


def _validate_zip_members(archive_path: Path, archive: zipfile.ZipFile) -> None:
    _validate_names(archive_path, archive.namelist())
    oversized = [
        info.filename
        for info in archive.infolist()
        if info.file_size > MAX_RELEASE_MEMBER_BYTES
    ]
    total_size = sum(info.file_size for info in archive.infolist())
    if oversized or total_size > MAX_RELEASE_UNCOMPRESSED_BYTES:
        raise ValueError(
            f"{archive_path.name} exceeds release content limits; oversized={oversized}"
        )
    links = [
        info.filename
        for info in archive.infolist()
        if stat.S_ISLNK((info.external_attr >> 16) & 0o177777)
    ]
    if links:
        raise ValueError(f"{archive_path.name} contains symbolic links: {sorted(links)}")


def _validate_tar_members(archive_path: Path, archive: tarfile.TarFile) -> None:
    members = archive.getmembers()
    _validate_names(archive_path, [member.name for member in members])
    oversized = [
        member.name for member in members if member.size > MAX_RELEASE_MEMBER_BYTES
    ]
    total_size = sum(member.size for member in members)
    if oversized or total_size > MAX_RELEASE_UNCOMPRESSED_BYTES:
        raise ValueError(
            f"{archive_path.name} exceeds release content limits; oversized={oversized}"
        )
    unsafe = [
        member.name
        for member in members
        if member.issym() or member.islnk() or member.isdev() or member.isfifo()
    ]
    if unsafe:
        raise ValueError(
            f"{archive_path.name} contains links or special files: {sorted(unsafe)}"
        )

    roots = {PurePosixPath(member.name).parts[0] for member in members if member.name}
    if len(roots) != 1:
        raise ValueError(f"{archive_path.name} must have one source-distribution root")
    root = next(iter(roots))
    unexpected: list[str] = []
    for member in members:
        parts = PurePosixPath(member.name).parts
        if len(parts) < 2 or parts[0] != root:
            continue
        relative = PurePosixPath(*parts[1:])
        if relative.parts[0] == "src":
            continue
        if len(relative.parts) == 1 and relative.name in ALLOWED_SDIST_ROOT_FILES:
            continue
        unexpected.append(member.name)
    if unexpected:
        raise ValueError(
            f"{archive_path.name} contains developer-only source files: {sorted(unexpected)}"
        )


def _validate_public_oauth_config(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name == "pulse/_product_oauth.json"]
        if len(names) != 1:
            raise ValueError(f"{wheel.name} has no product Google OAuth configuration")
        raw = archive.read(names[0])
    if len(raw) > 4096:
        raise ValueError(f"{wheel.name} has an oversized Google OAuth configuration")
    try:
        config = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{wheel.name} has invalid Google OAuth configuration") from error
    if not isinstance(config, dict) or set(config) != {
        "client_id",
        "client_secret",
        "redirect_uri",
    }:
        raise ValueError(f"{wheel.name} has invalid Google OAuth configuration fields")
    client_id = config.get("client_id")
    client_secret = config.get("client_secret")
    redirect_uri = config.get("redirect_uri")
    if not isinstance(client_id, str) or not GOOGLE_CLIENT_ID_PATTERN.fullmatch(client_id):
        raise ValueError(f"{wheel.name} has a missing or placeholder Google OAuth client ID")
    if not isinstance(client_secret, str) or not GOOGLE_CLIENT_SECRET_PATTERN.fullmatch(
        client_secret.encode("utf-8")
    ):
        raise ValueError(f"{wheel.name} has a missing Google Desktop OAuth credential")
    if not isinstance(redirect_uri, str):
        raise TypeError(f"{wheel.name} has a non-string Google OAuth redirect URI")
    try:
        parsed = urllib.parse.urlparse(redirect_uri)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{wheel.name} has an invalid Google OAuth redirect URI") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError(f"{wheel.name} has an unsafe Google OAuth redirect URI")


def _validate_content(archive_path: Path, members: list[tuple[str, bytes]]) -> None:
    violations: list[str] = []
    for name, content in members:
        normalized_name = name.replace("\\", "/")
        google_client_credential_outside_product_config = (
            not normalized_name.endswith("pulse/_product_oauth.json")
            and GOOGLE_CLIENT_SECRET_PATTERN.search(content)
        )
        if (
            PRIVATE_KEY_MARKER.search(content)
            or any(pattern.search(content) for pattern in INTERNAL_PATH_PATTERNS)
            or any(pattern.search(content) for pattern in HIGH_CONFIDENCE_SECRET_PATTERNS)
            or google_client_credential_outside_product_config
        ):
            violations.append(name)
    if violations:
        raise ValueError(
            f"{archive_path.name} contains secret material or internal paths: "
            f"{sorted(violations)}"
        )


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
        _validate_zip_members(wheel, archive)
        _validate_content(
            wheel,
            [(name, archive.read(name)) for name in archive.namelist() if not name.endswith("/")],
        )
    with tarfile.open(sdist, mode="r:gz") as archive:
        _validate_tar_members(sdist, archive)
        _validate_content(
            sdist,
            [
                (member.name, extracted.read())
                for member in archive.getmembers()
                if member.isfile() and (extracted := archive.extractfile(member)) is not None
            ],
        )

    version = _metadata_version(wheel)
    _validate_runtime_requirements(wheel)
    _validate_public_oauth_config(wheel)
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
    except (OSError, TypeError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"Release artifact verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
