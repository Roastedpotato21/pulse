from __future__ import annotations

import io
from types import SimpleNamespace

from rich.console import Console

from pulse import cli_ui
from pulse.edits import EditProposal


def _capture_console(monkeypatch) -> io.StringIO:
    stream = io.StringIO()
    monkeypatch.setattr(
        cli_ui,
        "console",
        Console(file=stream, force_terminal=False, color_system=None, width=120),
    )
    return stream


def test_untrusted_terminal_text_is_rendered_literally(monkeypatch) -> None:
    stream = _capture_console(monkeypatch)

    cli_ui.print_answer("[bold red]model supplied markup[/bold red]")
    cli_ui.print_success("Signed in as [link=https://evil.invalid]user[/link].")

    output = stream.getvalue()
    assert "[bold red]model supplied markup[/bold red]" in output
    assert "[link=https://evil.invalid]user[/link]" in output


def test_styled_help_matches_public_command_surface(monkeypatch) -> None:
    stream = _capture_console(monkeypatch)

    cli_ui.print_help_screen()

    output = stream.getvalue()
    assert "pulse version" in output
    assert "pulse keys set PROVIDER" in output
    assert "pulse keys rotate PROVIDER" in output
    assert "pulse login" in output and "pulse logout" in output
    assert "pulse register" not in output
    assert "pulse login USER PASS" not in output


def test_interactive_help_uses_slash_command_syntax(monkeypatch) -> None:
    stream = _capture_console(monkeypatch)

    cli_ui.print_help_screen(interactive=True)

    output = stream.getvalue()
    assert "/keys set PROVIDER" in output
    assert "/keys rotate PROVIDER" in output
    assert "/chat switch ID" in output
    assert "/status" in output
    assert "pulse keys set PROVIDER" not in output


def test_banner_identifies_pulse_as_a_coding_assistant(monkeypatch) -> None:
    stream = _capture_console(monkeypatch)

    cli_ui.print_banner()

    output = stream.getvalue()
    assert "Autonomous Coding Assistant" in output
    assert "Autonomous Project Assistant" not in output


def test_routine_message_panels_are_compact(monkeypatch) -> None:
    stream = _capture_console(monkeypatch)

    cli_ui.print_info("Short message")

    border = next(line for line in stream.getvalue().splitlines() if "+" in line)
    assert len(border) < 120


def test_startup_chat_history_replaces_active_conversation_card(monkeypatch) -> None:
    stream = _capture_console(monkeypatch)
    previous = SimpleNamespace(
        id="12345678-abcd",
        title="Authentication debugging",
        turn_count=8,
        updated_at="2026-09-05T02:27:00+00:00",
    )

    cli_ui.print_chat_history([previous])

    output = stream.getvalue()
    assert "Chat History" in output
    assert "Authentication debugging" in output
    assert "/chat switch <# or ID>" in output
    assert "Active Conversation" not in output


def test_edit_proposal_is_compact_and_does_not_dump_file_content(monkeypatch) -> None:
    stream = _capture_console(monkeypatch)
    proposal = EditProposal(
        file_path="site/index.html",
        before_content=None,
        after_content="secret full file body",
        reason="create page",
        unified_diff="--- a/site/index.html\n+++ b/site/index.html\n@@ -0,0 +1 @@\n+hello\n",
    )

    cli_ui.print_edit_proposal_summary(proposal)

    output = stream.getvalue()
    assert "site/index.html" in output
    assert "+1" in output and "-0" in output
    assert "secret full file body" not in output
    assert "+hello" not in output


def test_edit_summary_paginates_large_file_lists(monkeypatch) -> None:
    stream = _capture_console(monkeypatch)
    changes = [
        {"file_path": f"src/file_{index}.py", "unified_diff": f"+line {index}\n"}
        for index in range(10)
    ]

    page, pages = cli_ui.print_edit_summary(changes, page=1, page_size=8)

    output = stream.getvalue()
    assert (page, pages) == (1, 2)
    assert "src/file_0.py" in output
    assert "src/file_7.py" in output
    assert "src/file_8.py" not in output
    assert "/diff --page N" in output


def test_diff_review_prints_only_requested_page(monkeypatch) -> None:
    stream = _capture_console(monkeypatch)
    diff = "\n".join(f"+line {index}" for index in range(125))

    page, pages = cli_ui.print_diff_page(
        "src/large.py", diff, page=2, page_size=60
    )

    output = stream.getvalue()
    assert (page, pages) == (2, 3)
    assert "+line 60" in output and "+line 119" in output
    assert "+line 120" not in output
