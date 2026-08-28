"""Run the deterministic, versioned release evaluation corpus."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def run_suite(corpus_path: Path, report_path: Path) -> bool:
    corpus: dict[str, Any] = json.loads(corpus_path.read_text(encoding="utf-8"))
    if corpus.get("schema_version") != 1 or not corpus.get("cases"):
        raise ValueError("Evaluation corpus must use schema_version 1 and contain cases.")

    started = time.monotonic()
    results: list[dict[str, Any]] = []
    for case in corpus["cases"]:
        case_started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", case["pytest_node"]],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "kind": case["kind"],
                "passed": proc.returncode == 0,
                "duration_seconds": round(time.monotonic() - case_started, 3),
                "output": (proc.stdout + proc.stderr)[-4000:],
            }
        )

    duration = time.monotonic() - started
    task_results = [result for result in results if result["kind"] == "task"]
    policy_results = [result for result in results if result["kind"] == "policy_rejection"]
    task_rate = sum(result["passed"] for result in task_results) / len(task_results)
    policy_rate = sum(result["passed"] for result in policy_results) / len(policy_results)
    thresholds = corpus["thresholds"]
    provider_cost = 0.0
    passed = (
        task_rate >= thresholds["minimum_task_success_rate"]
        and policy_rate >= thresholds["minimum_policy_rejection_rate"]
        and duration <= thresholds["maximum_duration_seconds"]
        and provider_cost <= thresholds["maximum_provider_cost_usd"]
    )
    report = {
        "schema_version": 1,
        "suite": corpus["suite"],
        "passed": passed,
        "metrics": {
            "task_success_rate": task_rate,
            "policy_rejection_rate": policy_rate,
            "duration_seconds": round(duration, 3),
            "provider_cost_usd": provider_cost,
        },
        "thresholds": thresholds,
        "results": results,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return passed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        passed = run_suite(args.corpus, args.report)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"Release evaluation failed: {exc}", file=sys.stderr)
        return 2
    print(f"Release evaluation report: {args.report} ({'PASS' if passed else 'FAIL'})")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
