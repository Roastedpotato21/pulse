from __future__ import annotations

import argparse

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from pulse.cli import _build_parser
from pulse.interactive import SlashCommandCompleter, parse_slash_command


def _completions(text: str) -> set[str]:
    completer = SlashCommandCompleter(_build_parser())
    return {
        completion.text
        for completion in completer.get_completions(
            Document(text, cursor_position=len(text)), CompleteEvent()
        )
    }


def test_root_commands_complete_while_typing() -> None:
    assert "/keys" in _completions("/ke")
    assert "/status" in _completions("/st")


def test_nested_commands_and_options_are_completed_from_public_parser() -> None:
    assert "set" in _completions("/keys s")
    assert "switch" in _completions("/chat sw")
    assert "--production" in _completions("/doctor --p")
    assert {"local", "remote"} <= _completions("/doctor --target ")


def test_all_public_commands_are_reachable_from_interactive_completion() -> None:
    parser = _build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    expected = {f"/{command}" for command in subparsers.choices}

    assert expected <= _completions("/")


def test_slash_parser_preserves_quoted_arguments_without_shell_execution() -> None:
    assert parse_slash_command('/chat rename abc "Release planning"') == [
        "chat",
        "rename",
        "abc",
        "Release planning",
    ]


def test_slash_parser_preserves_windows_paths() -> None:
    assert parse_slash_command(r"/symbols C:\work tree\main.py") == [
        "symbols",
        r"C:\work",
        r"tree\main.py",
    ]
    assert parse_slash_command(r'/symbols "C:\work tree\main.py"') == [
        "symbols",
        r"C:\work tree\main.py",
    ]


def test_slash_parser_reports_unclosed_quotes() -> None:
    with pytest.raises(ValueError, match="Invalid command quoting"):
        parse_slash_command('/search "unfinished')
