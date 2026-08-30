from __future__ import annotations

import io

from rich.console import Console

from pulse import cli_ui


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
