from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Set


class ASTImpactAnalyzer:
    """Parses Python files to construct symbol cross-references and calculate refactoring impact."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self._symbol_map: Dict[str, Set[str]] = {}
        self._analyzed = False

    def build_symbol_map(self) -> None:
        self._symbol_map.clear()
        for path in self.workspace.rglob("*.py"):
            # Skip ignored directories
            if any(part in {".git", ".agent", ".venv", "venv", "__pycache__", ".pytest_cache"} for part in path.parts):
                continue
            relative_path = path.relative_to(self.workspace).as_posix()
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(content)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        self._add_symbol(node.name, relative_path)
                    elif isinstance(node, ast.Name):
                        self._add_symbol(node.id, relative_path)
                    elif isinstance(node, ast.Attribute):
                        self._add_symbol(node.attr, relative_path)
            except SyntaxError:
                continue
        self._analyzed = True

    def get_affected_files(self, symbol_name: str) -> List[str]:
        if not self._analyzed:
            self.build_symbol_map()
        return sorted(list(self._symbol_map.get(symbol_name, set())))

    def _add_symbol(self, symbol: str, file_path: str) -> None:
        if symbol not in self._symbol_map:
            self._symbol_map[symbol] = set()
        self._symbol_map[symbol].add(file_path)
