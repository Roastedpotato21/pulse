from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pulse.config import ModelConfig
from pulse.core.protocols import StreamChunk
from pulse.providers.base import BaseProvider
from scripts.run_provider_e2e import evaluate_provider


class FakeLiveProvider(BaseProvider):
    api_key_env_var = "FAKE_API_KEY"
    endpoint = "https://provider.invalid"

    def _headers(self) -> dict[str, str]:
        return {}

    def _parse_stream_chunk(self, payload_line: str) -> StreamChunk:
        return StreamChunk(content=payload_line)

    async def generate_stream(self, messages, temperature=0.2):
        yield StreamChunk(content="PULSE_", metadata={"raw": {}})
        yield StreamChunk(
            content="E2E_OK",
            metadata={"raw": {"usage": {"total_tokens": 14}}},
        )


def _suite() -> dict[str, object]:
    return {
        "schema_version": 1,
        "suite": "test",
        "sentinel": "PULSE_E2E_OK",
        "timeout_seconds": 1,
        "maximum_total_tokens": 20,
        "messages": [{"role": "user", "content": "test"}],
    }


def test_live_provider_evaluation_is_redacted_and_budgeted(tmp_path: Path) -> None:
    provider = FakeLiveProvider(
        ModelConfig("fake", "fake-model", 0.0, 64), tmp_path / ".env", api_key="secret"
    )
    report = asyncio.run(evaluate_provider(provider, _suite()))
    encoded = json.dumps(report)

    assert report["passed"] is True
    assert report["metrics"]["total_tokens"] == 14
    assert "secret" not in encoded
    assert "PULSE_E2E_OK" not in encoded


def test_live_provider_corpus_is_versioned_and_contains_no_project_data() -> None:
    corpus = json.loads(Path("evals/provider-live-v1.json").read_text(encoding="utf-8"))
    assert corpus["schema_version"] == 1
    assert corpus["provider"] == "openai"
    assert corpus["maximum_total_tokens"] <= 256
    assert all("File:" not in message["content"] for message in corpus["messages"])
