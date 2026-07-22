from __future__ import annotations

import pytest
from pulse.planner.dag_planner import DAGPlanner, DAGTaskNode
from pulse.refactor.impact_analyzer import ASTImpactAnalyzer


def test_dag_add_task():
    planner = DAGPlanner()
    node = planner.add_task("task_a", "Create model", target_files=["src/model.py"])
    assert node.id == "task_a"
    assert node.description == "Create model"
    assert "src/model.py" in node.target_files


def test_dag_duplicate_task_raises():
    planner = DAGPlanner()
    planner.add_task("task_a", "Create model")
    with pytest.raises(ValueError, match="Task node already exists"):
        planner.add_task("task_a", "Duplicate")


def test_dag_dependency_and_execution_order():
    planner = DAGPlanner()
    planner.add_task("task_a", "Step A")
    planner.add_task("task_b", "Step B")
    planner.add_task("task_c", "Step C")
    planner.add_dependency("task_b", "task_a")
    planner.add_dependency("task_c", "task_b")

    order = planner.get_execution_order()
    ids = [n.id for n in order]
    assert ids.index("task_a") < ids.index("task_b")
    assert ids.index("task_b") < ids.index("task_c")


def test_dag_cycle_detection():
    planner = DAGPlanner()
    planner.add_task("task_a", "A")
    planner.add_task("task_b", "B")
    planner.add_dependency("task_b", "task_a")
    with pytest.raises(ValueError, match="cycle"):
        planner.add_dependency("task_a", "task_b")


def test_dag_self_dependency_raises():
    planner = DAGPlanner()
    planner.add_task("task_a", "A")
    with pytest.raises(ValueError, match="cannot depend on itself"):
        planner.add_dependency("task_a", "task_a")


def test_dag_missing_node_dependency_raises():
    planner = DAGPlanner()
    planner.add_task("task_a", "A")
    with pytest.raises(KeyError):
        planner.add_dependency("task_a", "nonexistent")


def test_ast_impact_analyzer_symbol_detection(tmp_path):
    src = tmp_path / "mymodule.py"
    src.write_text(
        "class MyClass:\n    def my_method(self):\n        pass\n",
        encoding="utf-8",
    )

    analyzer = ASTImpactAnalyzer(tmp_path)
    affected = analyzer.get_affected_files("MyClass")
    assert any("mymodule.py" in f for f in affected)


def test_ast_impact_analyzer_no_match(tmp_path):
    src = tmp_path / "a.py"
    src.write_text("x = 1\n", encoding="utf-8")

    analyzer = ASTImpactAnalyzer(tmp_path)
    affected = analyzer.get_affected_files("NonExistentSymbol")
    assert affected == []


def test_ast_impact_analyzer_cross_reference(tmp_path):
    (tmp_path / "a.py").write_text("def shared_func(): pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("from a import shared_func\nshared_func()\n", encoding="utf-8")

    analyzer = ASTImpactAnalyzer(tmp_path)
    affected = analyzer.get_affected_files("shared_func")
    assert len(affected) >= 1
