import asyncio
from pathlib import Path

from pulse.repository import RepositoryIndex


def test_index_extracts_folders_imports_and_symbols_and_reuses_unchanged_files(tmp_path: Path) -> None:
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("import os\nfrom pulse.tools import StatusTool\n\nclass Demo:\n    async def run(self): pass\n", encoding="utf-8")
    index = RepositoryIndex(tmp_path)

    first = asyncio.run(index.index())
    second = asyncio.run(index.index())
    details = asyncio.run(index.details("src/example.py"))

    assert first.indexed == 1 and first.folders == 1
    assert second.indexed == 0 and second.unchanged == 1
    assert details and details.imports == ["os", "pulse.tools.StatusTool"]
    assert {(symbol.kind, symbol.name) for symbol in details.symbols} == {("class", "Demo"), ("function", "run")}


def test_search_finds_filename_and_semantic_symbols_and_reindexes_changes(tmp_path: Path) -> None:
    source = tmp_path / "service.py"
    source.write_text("def calculate_invoice(): pass\n", encoding="utf-8")
    index = RepositoryIndex(tmp_path)
    asyncio.run(index.index())

    results = asyncio.run(index.search("invoice"))
    source.write_text("def calculate_invoice(): pass\ndef send_receipt(): pass\n", encoding="utf-8")
    report = asyncio.run(index.index())
    symbols = asyncio.run(index.symbols("service.py"))

    assert results[0].path == "service.py"
    assert report.indexed == 1
    assert {symbol.name for symbol in symbols} == {"calculate_invoice", "send_receipt"}


def test_symbols_accepts_an_absolute_workspace_path_and_search_does_not_rewrite_unchanged_index(tmp_path: Path) -> None:
    source = tmp_path / "src" / "billing.py"
    source.parent.mkdir()
    source.write_text("def issue_invoice(): pass\n", encoding="utf-8")
    index = RepositoryIndex(tmp_path)

    asyncio.run(index.index())
    index_mtime = index.index_path.stat().st_mtime_ns
    assert asyncio.run(index.search("issue invoice"))[0].path == "src/billing.py"

    assert [symbol.name for symbol in asyncio.run(index.symbols(str(source)))] == ["issue_invoice"]
    assert index.index_path.stat().st_mtime_ns == index_mtime
