from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from pulse.tool_registry import ToolInvocation, ToolRegistry, ToolResult


class _DynamicTool:
    """A tool_registry-compatible wrapper around a dynamically loaded workspace tool script."""

    requires_permission = True

    def __init__(self, name: str, description: str, module: Any) -> None:
        self.name = name
        self.description = description
        self._module = module

    def matches(self, invocation: ToolInvocation) -> bool:
        return invocation.name == self.name

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        try:
            execute_fn = getattr(self._module, "execute", None)
            if execute_fn is None:
                return ToolResult(
                    f"Tool '{self.name}' has no `execute` function.",
                    metadata={"error": "missing_execute"},
                )
            import asyncio
            if asyncio.iscoroutinefunction(execute_fn):
                result = await execute_fn(invocation)
            else:
                result = execute_fn(invocation)

            if isinstance(result, ToolResult):
                return result
            return ToolResult(str(result))
        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                f"Dynamic tool '{self.name}' raised: {exc}",
                metadata={"error": str(exc)},
            )


class LocalToolLoader:
    """Automatically scans `.agent/tools/*.py` scripts in the workspace and registers
    them as dynamically available tools at runtime.

    Each tool script must define:
    - `NAME: str` — tool identifier
    - `DESCRIPTION: str` — human-readable description
    - `execute(invocation: ToolInvocation) -> ToolResult` — sync or async callable
    """

    def __init__(self, workspace: Path, registry: ToolRegistry) -> None:
        self._workspace = workspace.resolve()
        self._registry = registry
        self._tools_dir = self._workspace / ".agent" / "tools"

    def load(self) -> int:
        """Scan tools directory, import each script, and register valid tool modules.

        Returns the number of tools successfully loaded.
        """
        if not self._tools_dir.exists():
            return 0

        loaded = 0
        for script in sorted(self._tools_dir.glob("*.py")):
            if script.stem.startswith("_"):
                continue
            try:
                module = self._import_module(script)
                name: str = getattr(module, "NAME", script.stem)
                description: str = getattr(module, "DESCRIPTION", f"Local tool: {script.stem}")

                if not hasattr(module, "execute"):
                    continue

                tool = _DynamicTool(name=name, description=description, module=module)
                try:
                    self._registry.register(tool)
                    loaded += 1
                except ValueError:
                    pass  # Already registered; skip duplicate
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception:  # noqa: BLE001, S112
                continue

        return loaded

    @staticmethod
    def _import_module(script: Path) -> Any:
        module_name = f"_pulse_local_tool_{script.stem}"
        spec = importlib.util.spec_from_file_location(module_name, script)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load spec for {script}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
