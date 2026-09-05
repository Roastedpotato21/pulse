from __future__ import annotations

import argparse
import asyncio
import io
from types import SimpleNamespace

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from pulse.cli import _build_parser
from pulse.interactive import (
    InteractivePrompt,
    SlashCommandCompleter,
    parse_slash_command,
)


class _TerminalOutput(io.StringIO):
    @property
    def encoding(self) -> str:
        return "utf-8"


def _completion_items(text: str):
    completer = SlashCommandCompleter(_build_parser())
    return list(
        completer.get_completions(
            Document(text, cursor_position=len(text)), CompleteEvent()
        )
    )


def _completions(text: str) -> set[str]:
    return {completion.text for completion in _completion_items(text)}


def test_root_menu_prioritizes_real_interactive_commands_with_descriptions() -> None:
    completions = _completion_items("/")

    assert [completion.text for completion in completions[:6]] == [
        "/help",
        "/status",
        "/model",
        "/keys",
        "/clear",
        "/exit",
    ]
    descriptions = {
        completion.text: completion.display_meta_text for completion in completions
    }
    assert descriptions["/help"] == "Show all Pulse commands and usage examples."
    assert descriptions["/keys"] == (
        "Securely manage provider API keys in the OS credential vault."
    )


def test_root_commands_complete_while_typing() -> None:
    assert "/keys" in _completions("/ke")
    assert "/status" in _completions("/st")


def test_nested_commands_and_options_are_completed_from_public_parser() -> None:
    assert {"set", "rotate", "remove"} <= _completions("/keys ")
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


@pytest.mark.asyncio
async def test_pressing_slash_renders_menu_and_keeps_nested_completion_live(
    tmp_path,
) -> None:
    terminal = _TerminalOutput()
    output = Vt100_Output(
        terminal,
        get_size=lambda: Size(rows=30, columns=140),
        enable_bell=False,
        enable_cpr=False,
    )

    with create_pipe_input() as pipe_input:
        prompt = InteractivePrompt(
            tmp_path,
            _build_parser(),
            input=pipe_input,
            output=output,
        )
        task = asyncio.create_task(prompt.session.prompt_async("pulse> "))
        pipe_input.send_text("/")

        for _ in range(100):
            state = prompt.session.default_buffer.complete_state
            root_commands = {item.text for item in state.completions} if state else set()
            if {"/help", "/keys"} <= root_commands:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Pressing '/' did not open the root command menu")

        await asyncio.sleep(0.05)
        rendered = terminal.getvalue()
        assert "/help" in rendered
        assert "/keys" in rendered

        pipe_input.send_text("keys ")
        for _ in range(100):
            state = prompt.session.default_buffer.complete_state
            nested_commands = {item.text for item in state.completions} if state else set()
            if {"list", "set", "remove"} <= nested_commands:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Typing '/keys ' did not open the key-command menu")

        pipe_input.send_text("\r")
        assert (await asyncio.wait_for(task, timeout=1)).strip() == "/keys"


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


def test_prompt_does_not_render_the_conversation_title() -> None:
    rendered = None

    def capture_prompt(value):  # type: ignore[no-untyped-def]
        nonlocal rendered
        rendered = value
        return "hello"

    prompt = object.__new__(InteractivePrompt)
    prompt.session = SimpleNamespace(prompt=capture_prompt)

    assert prompt.read("a very long first conversation title") == "hello"
    assert rendered == [("class:prompt", "pulse"), ("", "> ")]
