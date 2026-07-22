from __future__ import annotations

import json
from pathlib import Path
import pytest

from pulse.telemetry import (
    BudgetExceededError,
    CostTracker,
    ModelPricing,
    TelemetryLogger,
)


def test_cost_tracker_token_counting_and_pricing():
    tracker = CostTracker()
    record1 = tracker.record_usage("gpt-4o-mini", 1000, 2000)

    assert record1.prompt_tokens == 1000
    assert record1.completion_tokens == 2000
    assert record1.total_tokens == 3000
    assert tracker.total_tokens == 3000
    assert tracker.total_cost > 0.0

    record2 = tracker.record_usage("gemini-2.0-flash", 500, 500)
    assert record2.total_tokens == 1000
    assert tracker.total_tokens == 4000


def test_cost_tracker_token_budget_exceeded():
    tracker = CostTracker(max_session_tokens=1000)
    tracker.record_usage("gpt-4o", 400, 400)  # 800 tokens OK

    with pytest.raises(BudgetExceededError) as exc_info:
        tracker.record_usage("gpt-4o", 200, 100)  # 800 + 300 = 1100 > 1000

    assert "Session token budget exceeded" in str(exc_info.value)


def test_cost_tracker_cost_budget_exceeded():
    tracker = CostTracker(max_session_cost=0.005)
    
    with pytest.raises(BudgetExceededError) as exc_info:
        tracker.record_usage("gpt-4o", 5000, 5000)  # High cost call

    assert "Session cost budget exceeded" in str(exc_info.value)


def test_cost_tracker_reset():
    tracker = CostTracker()
    tracker.record_usage("gpt-4o-mini", 500, 500)
    assert tracker.total_tokens == 1000
    assert tracker.total_cost > 0

    tracker.reset()
    assert tracker.total_tokens == 0
    assert tracker.total_cost == 0.0


def test_telemetry_logger_event_formatting(tmp_path: Path):
    log_file = tmp_path / "telemetry.jsonl"
    logger = TelemetryLogger(log_path=log_file)

    event = logger.log_step_execution(
        step=1,
        tool_name="read_file",
        duration_ms=123.45,
        success=True,
        file="src/pulse/cli.py",
    )

    assert event.event_type == "step_execution"
    assert event.step == 1
    assert event.duration_ms == 123.45
    assert event.metadata["tool_name"] == "read_file"
    assert event.metadata["success"] is True
    assert event.metadata["file"] == "src/pulse/cli.py"

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    parsed = json.loads(lines[0])
    assert parsed["event_type"] == "step_execution"
    assert parsed["step"] == 1
    assert parsed["duration_ms"] == 123.45
    assert parsed["metadata"]["file"] == "src/pulse/cli.py"
