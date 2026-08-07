from __future__ import annotations

from collections import Counter
from pathlib import Path

from pulse.memory.episodic import EpisodicMemory


class RuleSynthesizer:
    """Extracts repeated error resolution patterns from episodic memory and auto-generates guidelines into `.agent/rules`."""

    def __init__(
        self,
        memory: EpisodicMemory,
        rules_dir: Path | None = None,
        frequency_threshold: int = 2,
    ) -> None:
        self.memory = memory
        self.rules_dir = rules_dir or Path(".agent/rules")
        self.frequency_threshold = frequency_threshold

    def synthesize_rules(self) -> list[Path]:
        traces = self.memory.get_all_traces()
        if not traces:
            return []

        pattern_counter: Counter[str] = Counter()
        resolutions_by_error: dict[str, str] = {}

        for trace in traces:
            if not trace.error.strip() or not trace.resolution.strip():
                continue
            error_key = trace.error.strip().splitlines()[0][:60]
            pattern_counter[error_key] += 1
            resolutions_by_error[error_key] = trace.resolution.strip()

        self.rules_dir.mkdir(parents=True, exist_ok=True)
        generated_rules: list[Path] = []

        rule_idx = 1
        for error_key, count in pattern_counter.items():
            if count >= self.frequency_threshold:
                rule_file = self.rules_dir / f"rule_{rule_idx:03d}.md"
                content = (
                    f"# Auto-Synthesized Rule {rule_idx:03d}\n\n"
                    f"**Trigger Error Pattern:**\n```\n{error_key}\n```\n\n"
                    f"**Frequency Observed:** {count} times\n\n"
                    f"**Recommended Resolution Guideline:**\n{resolutions_by_error[error_key]}\n"
                )
                rule_file.write_text(content, encoding="utf-8")
                generated_rules.append(rule_file)
                rule_idx += 1

        return generated_rules
