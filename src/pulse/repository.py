"""Async incremental repository indexing and lexical semantic retrieval."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    kind: str
    line: int


@dataclass(slots=True)
class IndexedFile:
    path: str
    fingerprint: str
    imports: list[str] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class IndexReport:
    files: int
    folders: int
    indexed: int
    unchanged: int
    removed: int


@dataclass(frozen=True, slots=True)
class SearchResult:
    path: str
    score: float
    symbols: tuple[Symbol, ...]


class RepositoryIndex:
    """Persists metadata under `.agent` and reparses only changed files."""

    _IGNORED = {".git", ".agent", ".agents", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}

    def __init__(self, workspace: Path, index_path: Path | None = None) -> None:
        self.workspace = workspace.resolve()
        self.index_path = index_path or self.workspace / ".agent" / "repository-index.json"
        self._files: dict[str, IndexedFile] = {}
        self._folders: set[str] = set()
        self._loaded = False
        self._lock = asyncio.Lock()

    async def index(self) -> IndexReport:
        async with self._lock:
            return await asyncio.to_thread(self._index_sync)

    async def search(self, query: str, *, limit: int = 6) -> list[SearchResult]:
        """Return filename and lexical-semantic matches without exposing storage."""
        await self.index()
        query_terms = set(self._terms(query))
        query_lower = query.lower()
        results: list[SearchResult] = []
        for item in self._files.values():
            path_lower = item.path.lower()
            score = float(4 if query_lower and query_lower in path_lower else 0)
            score += 2 * len(query_terms.intersection(self._terms(Path(item.path).name)))
            score += len(query_terms.intersection(item.terms))
            score += 2 * len(query_terms.intersection({symbol.name.lower() for symbol in item.symbols}))
            if score:
                results.append(SearchResult(item.path, score, tuple(item.symbols)))
        return sorted(results, key=lambda result: (-result.score, result.path))[:limit]

    async def symbols(self, file_path: str) -> list[Symbol]:
        await self.index()
        return list(self._files.get(self._normalise_path(file_path), IndexedFile("", "")).symbols)

    async def details(self, file_path: str) -> IndexedFile | None:
        await self.index()
        return self._files.get(self._normalise_path(file_path))

    async def files(self) -> list[str]:
        await self.index()
        return sorted(self._files)

    def _index_sync(self) -> IndexReport:
        self._load()
        current: dict[str, Path] = {}
        folders: set[str] = set()
        for path in self.workspace.rglob("*"):
            relative = path.relative_to(self.workspace)
            if any(part in self._IGNORED for part in relative.parts):
                continue
            if path.is_dir():
                folders.add(relative.as_posix())
            elif path.is_file():
                current[relative.as_posix()] = path

        indexed = unchanged = 0
        for relative, path in current.items():
            fingerprint = self._fingerprint(path)
            existing = self._files.get(relative)
            if existing and existing.fingerprint == fingerprint:
                unchanged += 1
                continue
            self._files[relative] = self._parse(relative, path, fingerprint)
            indexed += 1
        removed_paths = set(self._files) - set(current)
        for relative in removed_paths:
            del self._files[relative]
        folders_changed = folders != self._folders
        self._folders = folders
        # Avoid rewriting the index on a no-op refresh.  This matters because
        # search refreshes the index before serving a result.
        if indexed or removed_paths or folders_changed or not self.index_path.exists():
            self._save()
        return IndexReport(len(self._files), len(folders), indexed, unchanged, len(removed_paths))

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.index_path.exists():
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            self._folders = set(raw.get("folders", []))
            for value in raw.get("files", []):
                value["symbols"] = [Symbol(**symbol) for symbol in value.get("symbols", [])]
                item = IndexedFile(**value)
                self._files[item.path] = item
        except (json.JSONDecodeError, TypeError, KeyError):
            self._files, self._folders = {}, set()

    def _save(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "folders": sorted(self._folders),
            "files": [asdict(item) for item in sorted(self._files.values(), key=lambda item: item.path)],
        }
        temporary_path = self.index_path.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary_path.replace(self.index_path)

    def _parse(self, relative: str, path: Path, fingerprint: str) -> IndexedFile:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        imports: list[str] = []
        symbols: list[Symbol] = []
        if path.suffix == ".py":
            try:
                tree = ast.parse(text)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        imports.extend(f"{node.module or ''}.{alias.name}".strip(".") for alias in node.names)
                    elif isinstance(node, ast.ClassDef):
                        symbols.append(Symbol(node.name, "class", node.lineno))
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(Symbol(node.name, "function", node.lineno))
            except SyntaxError:
                pass
        terms = sorted(set(self._terms(relative) + self._terms(text[:50_000]) + [symbol.name.lower() for symbol in symbols]))
        return IndexedFile(relative, fingerprint, imports, symbols, terms)

    @staticmethod
    def _fingerprint(path: Path) -> str:
        stat = path.stat()
        return hashlib.sha256(f"{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()

    @staticmethod
    def _terms(text: str) -> list[str]:
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", text.lower())
        # Preserve identifiers while also matching their snake_case components.
        return words + [part for word in words for part in word.split("_") if part]

    def _normalise_path(self, file_path: str) -> str:
        """Convert a CLI path to the portable path stored in the index."""
        candidate = Path(file_path)
        if candidate.is_absolute():
            try:
                candidate = candidate.resolve().relative_to(self.workspace)
            except ValueError:
                return ""
        return candidate.as_posix()
