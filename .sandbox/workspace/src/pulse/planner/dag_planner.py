from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DAGTaskNode:
    id: str
    description: str
    target_files: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    dependencies: set[str] = field(default_factory=set)


class DAGPlanner:
    """Decomposes multi-file features into a Directed Acyclic Graph of execution steps."""

    def __init__(self) -> None:
        self.nodes: dict[str, DAGTaskNode] = {}

    def add_task(
        self,
        task_id: str,
        description: str,
        target_files: list[str] | None = None,
        inputs: list[str] | None = None,
        outputs: list[str] | None = None,
        dependencies: set[str] | None = None,
    ) -> DAGTaskNode:
        if task_id in self.nodes:
            raise ValueError(f"Task node already exists: {task_id}")
        node = DAGTaskNode(
            id=task_id,
            description=description,
            target_files=target_files or [],
            inputs=inputs or [],
            outputs=outputs or [],
            dependencies=set(dependencies or []),
        )
        self.nodes[task_id] = node
        return node

    def add_dependency(self, task_id: str, depends_on_id: str) -> None:
        if task_id not in self.nodes or depends_on_id not in self.nodes:
            raise KeyError("Both task_id and depends_on_id must exist in DAG.")
        if task_id == depends_on_id:
            raise ValueError("A task cannot depend on itself.")
        self.nodes[task_id].dependencies.add(depends_on_id)
        if self._has_cycle():
            self.nodes[task_id].dependencies.remove(depends_on_id)
            raise ValueError("Adding this dependency creates a cycle in the DAG.")

    def get_execution_order(self) -> list[DAGTaskNode]:
        """Returns topological sort order of tasks for execution."""
        in_degree: dict[str, int] = {node_id: 0 for node_id in self.nodes}
        graph: dict[str, list[str]] = {node_id: [] for node_id in self.nodes}

        for node_id, node in self.nodes.items():
            for dep in node.dependencies:
                graph[dep].append(node_id)
                in_degree[node_id] += 1

        queue = [node_id for node_id, count in in_degree.items() if count == 0]
        order: list[DAGTaskNode] = []

        while queue:
            current_id = queue.pop(0)
            order.append(self.nodes[current_id])
            for neighbor in graph[current_id]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(order) != len(self.nodes):
            raise ValueError("Cycle detected in DAG planner tasks.")

        return order

    def _has_cycle(self) -> bool:
        try:
            self.get_execution_order()
            return False
        except ValueError:
            return True
