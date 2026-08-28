from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_release_evals import run_suite


def test_release_eval_corpus_has_required_categories() -> None:
    corpus = json.loads(Path("evals/release-v1.json").read_text(encoding="utf-8"))
    assert corpus["schema_version"] == 1
    assert {case["category"] for case in corpus["cases"]} == {
        "navigation",
        "bug_fix",
        "feature_work",
        "prompt_injection",
        "unsafe_tools",
        "crash_recovery",
        "refusal",
    }
    assert len({case["id"] for case in corpus["cases"]}) == len(corpus["cases"])


def test_release_eval_gate_writes_failure_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = {
        "schema_version": 1,
        "suite": "test",
        "thresholds": {
            "minimum_task_success_rate": 1.0,
            "minimum_policy_rejection_rate": 1.0,
            "maximum_duration_seconds": 120.0,
            "maximum_provider_cost_usd": 0.0,
        },
        "cases": [
            {"id": "task", "category": "test", "kind": "task", "pytest_node": "bad"},
            {
                "id": "policy",
                "category": "test",
                "kind": "policy_rejection",
                "pytest_node": "bad",
            },
        ],
    }
    corpus_path = tmp_path / "corpus.json"
    report_path = tmp_path / "report.json"
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")

    class FailedRun:
        returncode = 1
        stdout = "failed"
        stderr = ""

    monkeypatch.setattr("scripts.run_release_evals.subprocess.run", lambda *args, **kwargs: FailedRun())
    assert run_suite(corpus_path, report_path) is False
    assert json.loads(report_path.read_text(encoding="utf-8"))["passed"] is False
