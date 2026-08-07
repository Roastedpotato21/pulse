from __future__ import annotations

from pathlib import Path

from pulse.episodic import EpisodicMemory
from pulse.rule_synthesizer import RuleSynthesizer


def test_episodic_memory_log_trace(tmp_path: Path):
    mem = EpisodicMemory(db_path=tmp_path / "episodic.sqlite3")
    trace = mem.log_trace(
        prompt="Fix the import error",
        error="ModuleNotFoundError: No module named 'pulse'",
        resolution="Run `pip install -e .` to install in editable mode.",
    )
    assert trace.id > 0
    assert trace.prompt == "Fix the import error"
    assert "ModuleNotFoundError" in trace.error


def test_episodic_memory_search_similar_resolutions(tmp_path: Path):
    mem = EpisodicMemory(db_path=tmp_path / "episodic.sqlite3")
    mem.log_trace("Fix import", "ModuleNotFoundError: pulse", "Run pip install -e .")
    mem.log_trace("Fix syntax", "SyntaxError: invalid syntax", "Check line 12 for missing colon.")
    mem.log_trace("Debug timeout", "TimeoutError", "Increase request timeout.")

    results = mem.search_similar_resolutions("ModuleNotFoundError")
    assert len(results) == 1
    assert "pulse" in results[0].error

    results_all = mem.search_similar_resolutions("error")
    assert len(results_all) >= 1


def test_episodic_memory_get_all_traces(tmp_path: Path):
    mem = EpisodicMemory(db_path=tmp_path / "episodic.sqlite3")
    for i in range(3):
        mem.log_trace(f"prompt {i}", f"error {i}", f"resolution {i}")

    traces = mem.get_all_traces()
    assert len(traces) == 3
    assert traces[0].prompt == "prompt 0"
    assert traces[2].resolution == "resolution 2"


def test_rule_synthesizer_no_traces(tmp_path: Path):
    mem = EpisodicMemory(db_path=tmp_path / "episodic.sqlite3")
    synth = RuleSynthesizer(memory=mem, rules_dir=tmp_path / "rules")
    rules = synth.synthesize_rules()
    assert rules == []


def test_rule_synthesizer_generates_rules(tmp_path: Path):
    mem = EpisodicMemory(db_path=tmp_path / "episodic.sqlite3")
    error_text = "ModuleNotFoundError: No module named 'pulse'"
    resolution = "Run pip install -e . from workspace root."

    for _ in range(3):
        mem.log_trace("fix import", error_text, resolution)

    synth = RuleSynthesizer(memory=mem, rules_dir=tmp_path / "rules", frequency_threshold=2)
    rules = synth.synthesize_rules()

    assert len(rules) == 1
    rule_content = rules[0].read_text(encoding="utf-8")
    assert "ModuleNotFoundError" in rule_content
    assert "Frequency Observed" in rule_content
    assert resolution in rule_content


def test_rule_synthesizer_threshold_filtering(tmp_path: Path):
    mem = EpisodicMemory(db_path=tmp_path / "episodic.sqlite3")
    mem.log_trace("p1", "OneTimeError: rare", "Some rare fix.")
    mem.log_trace("p2", "FrequentError: common", "Common fix A.")
    mem.log_trace("p3", "FrequentError: common", "Common fix A.")

    synth = RuleSynthesizer(memory=mem, rules_dir=tmp_path / "rules", frequency_threshold=2)
    rules = synth.synthesize_rules()

    assert len(rules) == 1
    rule_content = rules[0].read_text(encoding="utf-8")
    assert "FrequentError" in rule_content
