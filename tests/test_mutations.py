import json
import os
from pathlib import Path

from pulse.mutations import MutationTracker


def test_tracker_records_create_modify_rename_and_delete_with_rollback_data(tmp_path: Path) -> None:
    tracker = MutationTracker(tmp_path)

    with tracker.transaction():
        (tmp_path / "created.txt").write_text("first\n", encoding="utf-8")
    with tracker.transaction():
        (tmp_path / "created.txt").write_text("second\n", encoding="utf-8")
    with tracker.transaction():
        (tmp_path / "created.txt").rename(tmp_path / "renamed.txt")
    with tracker.transaction():
        (tmp_path / "renamed.txt").unlink()

    events = list(tracker.history())
    assert [event["action"] for event in events] == ["create", "modify", "rename", "delete"]
    assert events[0]["after_content"] == f"first{os.linesep}"
    assert events[1]["before_content"] == f"first{os.linesep}"
    assert events[1]["after_content"] == f"second{os.linesep}"
    assert events[1]["unified_diff"].startswith("--- a/created.txt")
    assert len(events[1]["before_sha256"]) == 64
    assert events[2]["file_path"] == "created.txt -> renamed.txt"
    assert events[3]["before_content"] == f"second{os.linesep}"
    assert tracker.latest_transaction() == [events[-1]]


def test_tracker_marks_command_generated_files_and_writes_jsonl(tmp_path: Path) -> None:
    tracker = MutationTracker(tmp_path)
    with tracker.transaction(command="generate fixture"):
        (tmp_path / "generated.txt").write_text("generated", encoding="utf-8")

    event = list(tracker.history())[0]
    assert event["command"] == "generate fixture"
    assert event["generated_by_command"] is True
    assert json.loads(tracker.log_path.read_text(encoding="utf-8"))["file_path"] == "generated.txt"
