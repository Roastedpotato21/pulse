from __future__ import annotations

from pathlib import Path

import pytest

from pulse.audit import AuditLog
from pulse.orchestration import AgentOrchestrator
from pulse.repository import RepositoryIndex
from pulse.safety import RiskLevel, SafetyManager


def test_safety_manager_risk_assessment(tmp_path: Path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    sm = SafetyManager(audit_log=audit)

    assert sm.assess_risk("read_file", "test.py") == RiskLevel.LOW
    assert sm.assess_risk("edit_file", "test.py") == RiskLevel.MEDIUM
    assert sm.assess_risk("execute_command", "bash") == RiskLevel.HIGH


@pytest.mark.anyio
async def test_safety_manager_authorization(tmp_path: Path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    sm = SafetyManager(audit_log=audit, confirmation_callback=lambda action, risk: True)

    assert await sm.authorize("execute_command", "script.sh") is True
    assert len(audit.entries) == 1

    sm_denied = SafetyManager(audit_log=audit, confirmation_callback=lambda action, risk: False)
    assert await sm_denied.authorize("execute_command", "script.sh") is False


@pytest.mark.anyio
async def test_agent_orchestrator_deterministic(tmp_path: Path):
    (tmp_path / "foo.py").write_text("def hello_world(): pass\n", encoding="utf-8")
    repo = RepositoryIndex(tmp_path)
    await repo.index()

    orchestrator = AgentOrchestrator(repository=repo)
    res = await orchestrator.handle_request("list files")
    assert "foo.py" in res.content
    assert res.tool_name == "repository_list_files"
