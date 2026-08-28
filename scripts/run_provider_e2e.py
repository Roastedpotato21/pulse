"""Run a secret-safe live provider release evaluation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from pulse.config import ModelConfig
from pulse.providers.base import BaseProvider
from pulse.providers.manager import ProviderManager


async def evaluate_provider(
    provider: BaseProvider,
    suite: dict[str, Any],
) -> dict[str, Any]:
    started = time.monotonic()
    chunks = []
    total_tokens: int | None = None

    async def collect() -> None:
        nonlocal total_tokens
        async for chunk in provider.generate_stream(suite["messages"], temperature=0.0):
            chunks.append(chunk.content)
            raw = chunk.metadata.get("raw")
            if isinstance(raw, dict):
                usage = raw.get("usage")
                if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
                    total_tokens = usage["total_tokens"]

    await asyncio.wait_for(collect(), timeout=float(suite["timeout_seconds"]))
    content = "".join(chunks).strip()
    token_budget_passed = (
        total_tokens is None or total_tokens <= int(suite["maximum_total_tokens"])
    )
    passed = content == suite["sentinel"] and token_budget_passed
    return {
        "schema_version": 1,
        "suite": suite["suite"],
        "passed": passed,
        "provider": provider.config.provider,
        "model": provider.config.name,
        "metrics": {
            "duration_seconds": round(time.monotonic() - started, 3),
            "total_tokens": total_tokens,
            "token_budget_passed": token_budget_passed,
            "exact_sentinel_match": content == suite["sentinel"],
        },
    }


async def run(corpus_path: Path, report_path: Path) -> bool:
    suite: dict[str, Any] = json.loads(corpus_path.read_text(encoding="utf-8"))
    if suite.get("schema_version") != 1 or not isinstance(suite.get("messages"), list):
        raise ValueError("Provider corpus must use schema_version 1 and contain messages.")
    provider_name = os.environ.get("PULSE_E2E_PROVIDER") or str(suite["provider"])
    model_name = os.environ.get("PULSE_E2E_MODEL") or str(suite["default_model"])
    manager = ProviderManager(Path.cwd())
    provider = manager.create_provider(
        ModelConfig(
            provider=provider_name,
            name=model_name,
            temperature=0.0,
            max_tokens=64,
        ),
        Path.cwd() / ".env",
    )
    if not provider.is_configured:
        raise RuntimeError(f"{provider.api_key_env_var} is required for the live provider gate.")
    report = await evaluate_provider(provider, suite)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return bool(report["passed"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        passed = asyncio.run(run(args.corpus, args.report))
    except (TimeoutError, OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"Live provider evaluation failed: {exc}", file=sys.stderr)
        return 2
    print(f"Live provider evaluation: {'PASS' if passed else 'FAIL'} ({args.report})")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
