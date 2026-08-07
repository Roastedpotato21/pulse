from __future__ import annotations

import ast
from pathlib import Path

from pulse.context import ContextManager
from pulse.edits import ApprovalHandler, EditWorkflow
from pulse.mutations import MutationTracker
from pulse.reasoning import ReasoningEngine
from pulse.safety.safety_manager import SafetyManager
from pulse.task_manager import TaskManager


class PatchEngine:
    def __init__(
        self,
        edits: EditWorkflow,
        safety_manager: SafetyManager,
        mutations: MutationTracker,
        context_manager: ContextManager,
        reasoning_engine: ReasoningEngine,
        task_manager: TaskManager,
    ) -> None:
        self.edits = edits
        self.safety_manager = safety_manager
        self.mutations = mutations
        self.context_manager = context_manager
        self.reasoning_engine = reasoning_engine
        self.task_manager = task_manager

    def locate_node(self, file_path: str | Path, target_name: str) -> tuple[int, int] | None:
        """Find the start and end line numbers of a target function or class."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        
        content = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(content)
        except SyntaxError as e:
            raise ValueError(f"Cannot parse {file_path}: syntax error: {e}")

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):  # noqa: SIM102
                if node.name == target_name:
                    return (node.lineno, node.end_lineno)
        return None

    async def apply_patch(
        self,
        file_path: str,
        target_name: str,
        operation: str,
        content: str | None,
        approve: ApprovalHandler,
    ) -> bool:
        """Apply a patch operation to a target function or class."""
        path = Path(file_path)
        
        # 1. Authorize via SafetyManager
        is_safe = await self.safety_manager.authorize(
            action="patch", target=file_path, detail=f"Patch {target_name} ({operation})"
        )
        if not is_safe:
            return False

        # 2. Locate AST node
        loc = self.locate_node(path, target_name)
        if not loc:
            raise ValueError(f"Target '{target_name}' not found in {file_path}")
        start_line, end_line = loc

        # 3. Generate modified content
        original_content = path.read_text(encoding="utf-8")
        lines = original_content.splitlines(keepends=True)
        
        # Calculate indent of the target line
        target_line_str = lines[start_line - 1]
        indent = len(target_line_str) - len(target_line_str.lstrip())
        indent_str = target_line_str[:indent]

        # Prepare payload
        payload_lines = []
        if content:
            for line in content.splitlines(keepends=True):
                payload_lines.append(indent_str + line if line.strip() else line)
        
        if operation == "replace":
            new_lines = lines[:start_line - 1] + payload_lines + lines[end_line:]
        elif operation == "insert":
            # Insert before the node
            new_lines = lines[:start_line - 1] + payload_lines + lines[start_line - 1:]
        elif operation == "delete":
            new_lines = lines[:start_line - 1] + lines[end_line:]
        elif operation == "rename":
            if not content:
                raise ValueError("Rename operation requires new name in content")
            new_name = content.strip()
            # Simple replace first line definition
            first_line = lines[start_line - 1]
            first_line = first_line.replace(f"def {target_name}", f"def {new_name}")
            first_line = first_line.replace(f"class {target_name}", f"class {new_name}")
            new_lines = lines[:start_line - 1] + [first_line] + lines[start_line:]
        else:
            raise ValueError(f"Unknown patch operation: {operation}")

        modified_content = "".join(new_lines)

        # 4. Validate syntax
        try:
            ast.parse(modified_content)
        except SyntaxError as e:
            # Syntax validation failed
            raise ValueError(f"Patch would result in invalid Python syntax: {e}")

        # 5. Apply via EditWorkflow
        with self.mutations.transaction(command="pulse patch"):
            result = await self.edits.request_and_apply(
                file_path=file_path,
                content=modified_content,
                reason=f"Patch Engine: {operation} {target_name}",
                approve=approve
            )
            
            if not result.applied:
                # Need rollback if failed? Not applied means we didn't touch FS yet, just rejected.
                return False
                
        return True
