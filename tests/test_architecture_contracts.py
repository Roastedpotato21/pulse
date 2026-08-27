"""Architecture boundary and lifecycle-contract regression tests."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "pulse"


def _imports(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def _package_imports(package: str) -> set[str]:
    package_path = SOURCE_ROOT / package
    imports: set[str] = set()
    for module_path in package_path.rglob("*.py"):
        imports.update(_imports(module_path))
    return imports


def _forbidden(imports: set[str], prefixes: tuple[str, ...]) -> set[str]:
    return {
        imported
        for imported in imports
        if any(imported == prefix or imported.startswith(f"{prefix}.") for prefix in prefixes)
    }


def test_architecture_contract_documents_durable_lifecycles() -> None:
    contract = (SOURCE_ROOT.parents[1] / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for required_term in (
        "Task Lifecycle Contract",
        "Sandbox Lifecycle Contract",
        "Lease fence",
        "RECOVERY_PENDING",
        "RECOVERY_REQUIRED",
        "Dependency Rules Enforced in Tests",
    ):
        assert required_term in contract


def test_core_package_remains_domain_only() -> None:
    imports = _package_imports("core")
    assert not _forbidden(
        imports,
        (
            "pulse.agent",
            "pulse.agent_manager",
            "pulse.cli",
            "pulse.orchestration",
            "pulse.providers",
            "pulse.sandbox",
            "pulse.rpc",
            "pulse.task_manager",
        ),
    )


def test_provider_package_cannot_reach_execution_or_orchestration() -> None:
    imports = _package_imports("providers")
    assert not _forbidden(
        imports,
        (
            "pulse.agent",
            "pulse.agent_manager",
            "pulse.orchestration",
            "pulse.sandbox",
            "pulse.task_manager",
        ),
    )


def test_sandbox_package_cannot_reach_agent_or_provider_layers() -> None:
    imports = _package_imports("sandbox")
    assert not _forbidden(
        imports,
        (
            "pulse.agent",
            "pulse.agent_manager",
            "pulse.orchestration",
            "pulse.planner",
            "pulse.providers",
        ),
    )


def test_task_manager_is_independent_of_ui_or_provider_implementations() -> None:
    imports = _imports(SOURCE_ROOT / "task_manager.py")
    assert not _forbidden(
        imports,
        (
            "pulse.agent",
            "pulse.agent_manager",
            "pulse.cli",
            "pulse.cli_ui",
            "pulse.orchestration",
            "pulse.providers",
            "pulse.rpc",
        ),
    )
