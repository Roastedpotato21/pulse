"""Unit tests for pulse.conversations.manager — ConversationManager."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pulse.conversations.manager import (
    Conversation,
    ConversationManager,
    ConversationTurn,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cm(tmp_path: Path) -> ConversationManager:
    """Fresh ConversationManager backed by a temp directory."""
    return ConversationManager(workspace=tmp_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_returns_conversation(self, cm: ConversationManager) -> None:
        conv = cm.create()
        assert isinstance(conv, Conversation)
        assert conv.title == "New Conversation"
        assert len(conv.id) == 36  # UUID length

    def test_create_with_title(self, cm: ConversationManager) -> None:
        conv = cm.create(title="My Project")
        assert conv.title == "My Project"

    def test_create_sets_active(self, cm: ConversationManager) -> None:
        conv = cm.create(title="Active Test")
        active = cm.get_active()
        assert active is not None
        assert active.id == conv.id

    def test_create_stored_in_list(self, cm: ConversationManager) -> None:
        conv = cm.create(title="Listed")
        all_convs = cm.list_all()
        assert any(c.id == conv.id for c in all_convs)


class TestAutoTitle:
    def test_short_message(self, cm: ConversationManager) -> None:
        conv = cm.create()
        updated = cm.auto_title(conv.id, "Fix the login bug")
        assert updated.title == "Fix the login bug"

    def test_long_message_truncated(self, cm: ConversationManager) -> None:
        conv = cm.create()
        long_msg = "a" * 80
        updated = cm.auto_title(conv.id, long_msg)
        assert len(updated.title) <= 62  # 60 chars + ellipsis

    def test_auto_title_with_ellipsis(self, cm: ConversationManager) -> None:
        conv = cm.create()
        long_msg = "How do I refactor the authentication module in this project to use JWT tokens"
        updated = cm.auto_title(conv.id, long_msg)
        assert updated.title.endswith("…")

    def test_whitespace_normalized(self, cm: ConversationManager) -> None:
        conv = cm.create()
        # auto_title normalizes consecutive whitespace to single spaces
        updated = cm.auto_title(conv.id, "  hello   world  ")
        assert updated.title == "hello world"


class TestListAll:
    def test_empty(self, cm: ConversationManager) -> None:
        assert cm.list_all() == []

    def test_ordered_by_updated_at(self, cm: ConversationManager) -> None:
        cm.create(title="First")
        cm.create(title="Second")
        c3 = cm.create(title="Third")
        # Newest created/switched first
        ids = [c.id for c in cm.list_all()]
        assert ids[0] == c3.id

    def test_turn_count_included(self, cm: ConversationManager) -> None:
        conv = cm.create(title="With turns")
        cm.add_turn(conv.id, "user", "hello")
        cm.add_turn(conv.id, "assistant", "hi!")
        listed = cm.list_all()
        assert listed[0].turn_count == 2


class TestGet:
    def test_get_existing(self, cm: ConversationManager) -> None:
        conv = cm.create(title="GetMe")
        result = cm.get(conv.id)
        assert result is not None
        assert result.title == "GetMe"

    def test_get_nonexistent(self, cm: ConversationManager) -> None:
        result = cm.get("00000000-0000-0000-0000-000000000000")
        assert result is None


class TestRename:
    def test_rename(self, cm: ConversationManager) -> None:
        conv = cm.create(title="Old")
        updated = cm.rename(conv.id, "New Title")
        assert updated.title == "New Title"

    def test_rename_strips_whitespace(self, cm: ConversationManager) -> None:
        conv = cm.create()
        updated = cm.rename(conv.id, "   Trimmed   ")
        assert updated.title == "Trimmed"

    def test_rename_nonexistent_raises(self, cm: ConversationManager) -> None:
        with pytest.raises(ValueError):
            cm.rename("00000000-0000-0000-0000-000000000000", "X")


class TestDelete:
    def test_delete_removes_from_list(self, cm: ConversationManager) -> None:
        conv = cm.create(title="ToDelete")
        cm.delete(conv.id)
        assert cm.get(conv.id) is None
        assert not any(c.id == conv.id for c in cm.list_all())

    def test_delete_cascades_turns(self, cm: ConversationManager) -> None:
        conv = cm.create()
        cm.add_turn(conv.id, "user", "hello")
        cm.delete(conv.id)
        # No error; turns table should be empty for that conv
        # (can't fetch them since conv is gone, but get() returns None)
        assert cm.get(conv.id) is None

    def test_delete_clears_active(self, cm: ConversationManager) -> None:
        conv = cm.create(title="WillBeGone")
        assert cm.get_active() is not None
        cm.delete(conv.id)
        assert cm.get_active() is None


class TestSwitch:
    def test_switch_changes_active(self, cm: ConversationManager) -> None:
        c1 = cm.create(title="One")
        cm.create(title="Two")
        cm.switch(c1.id)
        active = cm.get_active()
        assert active is not None
        assert active.id == c1.id

    def test_switch_nonexistent_raises(self, cm: ConversationManager) -> None:
        with pytest.raises(ValueError):
            cm.switch("00000000-0000-0000-0000-000000000000")


class TestGetActive:
    def test_no_active_on_fresh_db(self, cm: ConversationManager) -> None:
        assert cm.get_active() is None

    def test_active_restored_after_switch(self, cm: ConversationManager) -> None:
        conv = cm.create(title="Persistent")
        # Simulate a new manager instance (same DB)
        cm2 = ConversationManager(workspace=cm.workspace, database_path=cm.database_path)
        active = cm2.get_active()
        assert active is not None
        assert active.id == conv.id

    def test_creating_a_chat_for_a_new_cli_launch_does_not_delete_history(
        self, cm: ConversationManager
    ) -> None:
        previous = cm.create(title="Previous chat")
        cm.add_turn(previous.id, "user", "Remember this")

        fresh = cm.create()

        assert cm.get_active() == fresh
        assert fresh.turn_count == 0
        assert any(conv.id == previous.id for conv in cm.list_all())


class TestTurns:
    def test_add_and_get_turns(self, cm: ConversationManager) -> None:
        conv = cm.create()
        cm.add_turn(conv.id, "user", "Hello")
        cm.add_turn(conv.id, "assistant", "Hi there!")
        turns = cm.get_turns(conv.id)
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[0].content == "Hello"
        assert turns[1].role == "assistant"
        assert turns[1].content == "Hi there!"

    def test_turn_is_conversation_turn(self, cm: ConversationManager) -> None:
        conv = cm.create()
        turn = cm.add_turn(conv.id, "user", "test")
        assert isinstance(turn, ConversationTurn)

    def test_add_turn_bumps_updated_at(self, cm: ConversationManager) -> None:
        conv = cm.create()
        old_updated = conv.updated_at
        cm.add_turn(conv.id, "user", "bump")
        fresh = cm.get(conv.id)
        assert fresh is not None
        assert fresh.updated_at >= old_updated

    def test_empty_turns(self, cm: ConversationManager) -> None:
        conv = cm.create()
        assert cm.get_turns(conv.id) == []


class TestSearch:
    def test_search_by_title(self, cm: ConversationManager) -> None:
        cm.create(title="Authentication Refactor")
        cm.create(title="UI Redesign")
        results = cm.search("Authentication")
        assert len(results) == 1
        assert results[0].title == "Authentication Refactor"

    def test_search_by_turn_content(self, cm: ConversationManager) -> None:
        conv = cm.create(title="Generic Chat")
        cm.add_turn(conv.id, "user", "How do I implement JWT tokens?")
        results = cm.search("JWT")
        assert len(results) == 1
        assert results[0].id == conv.id

    def test_search_empty_query_returns_all(self, cm: ConversationManager) -> None:
        cm.create(title="A")
        cm.create(title="B")
        results = cm.search("")
        assert len(results) == 2

    def test_search_no_match(self, cm: ConversationManager) -> None:
        cm.create(title="Nothing relevant")
        results = cm.search("xyzzy_no_match_1234")
        assert results == []

    def test_search_case_insensitive(self, cm: ConversationManager) -> None:
        cm.create(title="Refactoring Sprint")
        results = cm.search("refactoring")
        assert len(results) == 1


class TestExportMarkdown:
    def test_export_md_creates_file(self, cm: ConversationManager, tmp_path: Path) -> None:
        conv = cm.create(title="Export Test")
        cm.add_turn(conv.id, "user", "What is a monad?")
        cm.add_turn(conv.id, "assistant", "A monad is a design pattern...")
        out = cm.export(conv.id, output_path=tmp_path / "out.md", fmt="md")
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "# Export Test" in content
        assert "What is a monad?" in content
        assert "A monad is a design pattern" in content

    def test_export_md_structure(self, cm: ConversationManager, tmp_path: Path) -> None:
        conv = cm.create(title="Structured")
        cm.add_turn(conv.id, "user", "Hello")
        out = cm.export(conv.id, output_path=tmp_path / "structured.md", fmt="md")
        content = out.read_text(encoding="utf-8")
        assert "**You**" in content
        assert "---" in content


class TestExportJson:
    def test_export_json_creates_file(self, cm: ConversationManager, tmp_path: Path) -> None:
        conv = cm.create(title="JSON Export")
        cm.add_turn(conv.id, "user", "Question?")
        cm.add_turn(conv.id, "assistant", "Answer!")
        out = cm.export(conv.id, output_path=tmp_path / "out.json", fmt="json")
        assert out.exists()

    def test_export_json_structure(self, cm: ConversationManager, tmp_path: Path) -> None:
        conv = cm.create(title="JSON Test")
        cm.add_turn(conv.id, "user", "hi")
        out = cm.export(conv.id, output_path=tmp_path / "out.json", fmt="json")
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["id"] == conv.id
        assert data["title"] == "JSON Test"
        assert isinstance(data["turns"], list)
        assert data["turns"][0]["role"] == "user"
        assert data["turns"][0]["content"] == "hi"

    def test_export_nonexistent_raises(self, cm: ConversationManager, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            cm.export("00000000-0000-0000-0000-000000000000", output_path=tmp_path / "x.md")

    def test_export_auto_filename(self, cm: ConversationManager) -> None:
        conv = cm.create(title="Auto File Name")
        out = cm.export(conv.id, fmt="md")
        assert out.exists()
        assert "Auto_File_Name" in out.name or "Auto" in out.name
        out.unlink()
