"""Responsive terminal input for the interactive Pulse shell."""

from __future__ import annotations

import argparse
import shlex
from collections.abc import Iterable
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML, FormattedText
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style

_STYLE = Style.from_dict(
    {
        "prompt": "bold ansicyan",
        "conversation": "ansibrightblack",
        "bottom-toolbar": "bg:#20242b #aeb6c2",
        "completion-menu.completion": "bg:#20242b #d7dae0",
        "completion-menu.completion.current": "bg:#146b8c #ffffff bold",
        "completion-menu.meta.completion": "bg:#20242b #8f9aa8",
        "completion-menu.meta.completion.current": "bg:#146b8c #ffffff",
    }
)


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    return next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )


def _command_help(action: argparse._SubParsersAction) -> dict[str, str]:
    return {
        choice.dest: choice.help or ""
        for choice in action._choices_actions
    }


class SlashCommandCompleter(Completer):
    """Complete the live argparse command tree after a leading slash."""

    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self.parser = parser

    def get_completions(self, document: Document, complete_event: object) -> Iterable[Completion]:
        del complete_event
        raw = document.text_before_cursor
        if not raw.startswith("/") or "\n" in raw:
            return

        body = raw[1:]
        parts = body.split()
        fragment = "" if body.endswith((" ", "\t")) else (parts[-1] if parts else "")
        consumed = parts if not fragment else parts[:-1]
        parser = self.parser

        # Walk through each nested argparse subcommand already entered.
        index = 0
        while index < len(consumed):
            subcommands = _subparsers(parser)
            token = consumed[index]
            if subcommands and token in subcommands.choices:
                parser = subcommands.choices[token]
            index += 1

        previous = consumed[-1] if consumed else ""
        for action in parser._actions:
            if previous in action.option_strings and action.choices:
                for choice in action.choices:
                    value = str(choice)
                    if value.startswith(fragment):
                        yield Completion(value, start_position=-len(fragment))
                return

        candidates: dict[str, str] = {}
        subcommands = _subparsers(parser)
        if subcommands:
            candidates.update(_command_help(subcommands))
        for action in parser._actions:
            if action.help == argparse.SUPPRESS:
                continue
            for option in action.option_strings:
                candidates[option] = action.help or ""

        root_position = parser is self.parser and not consumed
        for value, description in candidates.items():
            shown = f"/{value}" if root_position else value
            typed = raw if root_position else fragment
            if shown.startswith(typed):
                yield Completion(
                    shown,
                    start_position=-len(typed),
                    display_meta=description,
                )


def build_key_bindings() -> KeyBindings:
    """Return shell-like bindings while keeping Enter fast and predictable."""
    bindings = KeyBindings()

    @bindings.add("enter")
    def _submit(event: object) -> None:
        event.current_buffer.validate_and_handle()  # type: ignore[attr-defined]

    @bindings.add("escape", "enter")
    def _newline(event: object) -> None:
        event.current_buffer.insert_text("\n")  # type: ignore[attr-defined]

    @bindings.add("c-space")
    def _complete(event: object) -> None:
        event.current_buffer.start_completion(select_first=False)  # type: ignore[attr-defined]

    @bindings.add("/")
    def _open_command_menu(event: object) -> None:
        buffer = event.current_buffer  # type: ignore[attr-defined]
        buffer.insert_text("/")
        if buffer.text == "/":
            buffer.start_completion(select_first=False)

    @bindings.add("tab")
    def _tab_complete(event: object) -> None:
        buffer = event.current_buffer  # type: ignore[attr-defined]
        if buffer.complete_state:
            buffer.complete_next()
        else:
            buffer.start_completion(select_first=True)

    @bindings.add("s-tab")
    def _previous_completion(event: object) -> None:
        buffer = event.current_buffer  # type: ignore[attr-defined]
        if buffer.complete_state:
            buffer.complete_previous()
        else:
            buffer.start_completion(select_first=True)

    @bindings.add("c-c")
    def _cancel(event: object) -> None:
        buffer = event.current_buffer  # type: ignore[attr-defined]
        if buffer.text:
            buffer.reset()
        else:
            event.app.exit(exception=KeyboardInterrupt)  # type: ignore[attr-defined]

    return bindings


class InteractivePrompt:
    """A reusable, testable prompt session with completion and safe history."""

    def __init__(self, workspace: Path, parser: argparse.ArgumentParser) -> None:
        history_dir = workspace / ".pulse"
        try:
            history_dir.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(history_dir / "history"))
        except OSError:
            history = InMemoryHistory()

        self.session: PromptSession[str] = PromptSession(
            completer=SlashCommandCompleter(parser),
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            key_bindings=build_key_bindings(),
            style=_STYLE,
            complete_while_typing=True,
            complete_in_thread=False,
            complete_style=CompleteStyle.COLUMN,
            reserve_space_for_menu=12,
            enable_history_search=True,
            multiline=True,
            prompt_continuation=lambda width, line, wrap: " " * max(0, width - 2) + "· ",
            bottom_toolbar=HTML(
                " <b>Tab</b> complete  <b>↑↓</b> history  <b>Ctrl-R</b> search  "
                "<b>Alt-Enter</b> newline  <b>Ctrl-C</b> clear  <b>/help</b> commands "
            ),
        )

    def read(self, conversation: str) -> str:
        label = conversation[:28]
        return self.session.prompt(
            FormattedText(
                [
                    ("class:prompt", "pulse"),
                    ("class:conversation", f" [{label}]"),
                    ("", "> "),
                ]
            )
        ).strip()


def parse_slash_command(value: str) -> list[str]:
    """Parse a quoted slash command without shell execution or path mangling."""
    if not value.startswith("/"):
        raise ValueError("Interactive commands must start with '/'.")
    try:
        lexer = shlex.shlex(value[1:], posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        # A backslash is a path separator in a cross-platform project CLI, not
        # a shell escape. Quoting remains available for arguments with spaces.
        lexer.escape = ""
        return list(lexer)
    except ValueError as error:
        raise ValueError(f"Invalid command quoting: {error}") from error
