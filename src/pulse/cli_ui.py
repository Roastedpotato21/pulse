"""Premium Rich terminal UI for Pulse.

All original function signatures are preserved.
New exports: print_banner, print_auth_prompt, print_signed_in,
             print_help_screen, print_session_footer, thinking_spinner,
             print_provider_selection, print_model_selection, print_provider_changed_card,
             print_current_model_card, print_all_models_list,
             print_chat_list, print_chat_card, print_chat_created, print_chat_switched,
             print_chat_exported, print_chat_search_results.
"""
from __future__ import annotations

import itertools
import subprocess
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from rich import box
from rich.align import Align
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from pulse import __version__

# ── Global console ────────────────────────────────────────────────────────────
console = Console()

_VERSION = __version__


# ── Terminal capability helpers ───────────────────────────────────────────────

def _is_dumb() -> bool:
    """Return True when stdout is a pipe, file, or dumb terminal."""
    return not console.is_terminal or getattr(console, "is_dumb_terminal", False)


def _ok() -> str:
    return "[OK]" if _is_dumb() else "\u2713"      # ✓


def _err() -> str:
    return "[ERR]" if _is_dumb() else "\u2717"     # ✗


def _warn() -> str:
    return "[WARN]" if _is_dumb() else "\u26a0"    # ⚠


def _box_style() -> box.Box:
    return box.ASCII if _is_dumb() else box.ROUNDED


# ── Core helpers ──────────────────────────────────────────────────────────────

def _panel(title: str | None, content: Any, style: str = "") -> Panel:
    """Adaptive panel — ROUNDED on rich terminals, ASCII on dumb ones."""
    return Panel(
        content,
        title=title,
        style=style,
        box=_box_style(),
        padding=(0, 1),
    )


def _rule(title: str | None = None) -> Rule:
    chars = "-" if _is_dumb() else "\u2500"        # ─
    return Rule(title, characters=chars) if title else Rule(characters=chars)


# ── Standard message functions ────────────────────────────────────────────────

def print_info(message: str) -> None:
    """Informational message — cyan bordered panel."""
    console.print(_panel("Info", escape(message), style="cyan"))
    console.print()


def print_success(message: str) -> None:
    """Success message — green panel with check mark."""
    console.print(
        _panel("Success", f"[bold green]{_ok()}[/bold green]  {escape(message)}", style="green")
    )
    console.print()


def print_warning(message: str) -> None:
    """Warning message — yellow panel."""
    console.print(
        _panel("Warning", f"[bold yellow]{_warn()}[/bold yellow]  {escape(message)}", style="yellow")
    )
    console.print()


def print_error(message: str) -> None:
    """Error message — red panel."""
    console.print(
        _panel("Error", f"[bold red]{_err()}[/bold red]  {escape(message)}", style="red")
    )
    console.print()


def print_question(message: str) -> None:
    """User question — magenta panel."""
    console.print(
        _panel("Question", f"[bold magenta]?[/bold magenta]  {escape(message)}", style="magenta")
    )
    console.print()


def print_answer(message: str) -> None:
    """Agent answer — bright_cyan panel."""
    console.print(_panel("Answer", escape(message), style="bright_cyan"))
    console.print()


def print_summary(message: str) -> None:
    """Summary panel — dim."""
    console.print(_panel("Summary", escape(message), style="dim"))
    console.print()


def print_verification(message: str) -> None:
    """Proposed-edit verification — blue panel."""
    console.print(_panel("Verification", escape(message), style="blue"))
    console.print()


def print_prompt(message: str) -> None:
    """Neutral prompt panel."""
    console.print(_panel(None, escape(message)))
    console.print()


def print_cli_output(content: Any, title: str | None = None, style: str = "") -> None:
    """Generic CLI output. Accepts plain strings and Rich renderables (Tables, etc.)."""
    if isinstance(content, str):
        content = escape(content)
    console.print(_panel(title, content, style=style))
    console.print()


# ── Banner ────────────────────────────────────────────────────────────────────

_LOGO_RICH = (
    "[bold cyan]██████╗ ██╗   ██╗██╗     ███████╗███████╗[/bold cyan]\n"
    "[bold cyan]██╔══██╗██║   ██║██║     ██╔════╝██╔════╝[/bold cyan]\n"
    "[bold cyan]██████╔╝██║   ██║██║     ███████╗█████╗  [/bold cyan]\n"
    "[bold cyan]██╔═══╝ ██║   ██║██║     ╚════██║██╔══╝  [/bold cyan]\n"
    "[bold cyan]██║     ╚██████╔╝███████╗███████║███████╗[/bold cyan]\n"
    "[bold cyan]╚═╝      ╚═════╝ ╚══════╝╚══════╝╚══════╝[/bold cyan]\n\n"
    "                [bold white]Pulse AI[/bold white]\n"
    f"      [dim]Autonomous Project Assistant  v{_VERSION}[/dim]"
)

_LOGO_ASCII = (
    " ____  _   _ _     ____  _____\n"
    "|  _ \\| | | | |   / ___||  ___|\n"
    "| |_) | | | | |   \\___ \\| |_\n"
    "|  __/| |_| | |___ ___) |  _|\n"
    "|_|    \\___/|_____|____/|_|\n\n"
    "                Pulse AI\n"
    f"      Autonomous Project Assistant  v{_VERSION}"
)


def print_banner(config: Any = None, runtime: Any = None) -> None:
    """Full-width startup banner with optional status grid."""
    logo = _LOGO_ASCII if _is_dumb() else _LOGO_RICH
    console.print()
    console.print(Align.center(logo))
    console.print()

    if config is not None:
        _print_status_grid(config, runtime)

    console.print(_rule())
    console.print()


def _print_status_grid(config: Any, runtime: Any = None) -> None:
    """Two-column status grid shown below the banner."""
    auth = getattr(runtime, "auth", None)
    auth_text = "Not signed in"
    auth_style = "yellow"

    if auth is not None and auth.is_authenticated():
        info = auth.get_current_user_info()
        if info:
            _uname, display_name, email = info
            if display_name and email:
                auth_text = f"{escape(display_name)} ({escape(email)})"
            elif email:
                auth_text = escape(email)
            else:
                auth_text = f"@{escape(str(auth.current_user()))}"
        else:
            auth_text = f"@{escape(str(auth.current_user()))}"
        auth_style = "bold green"

    project_path = escape(str(getattr(getattr(config, "sandbox", None), "workspace_root", ".")))
    if len(project_path) > 40:
        project_path = "..." + project_path[-37:]

    grid = Table.grid(padding=(0, 3))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(style="dim", no_wrap=True, min_width=4)
    grid.add_column(no_wrap=True)

    grid.add_row("Provider", f"[cyan]{escape(str(config.model.provider))}[/cyan]",
                 "Auth", f"[{auth_style}]{auth_text}[/{auth_style}]")
    grid.add_row("Model",    f"[cyan]{escape(str(config.model.name))}[/cyan]",
                 "Mode", f"[cyan]{escape(str(config.mode))}[/cyan]")
    grid.add_row("Project",  f"[dim]{project_path}[/dim]",
                 "Version", f"[dim]v{_VERSION}[/dim]")

    console.print(Align.center(grid))
    console.print()


# ── Auth prompt ───────────────────────────────────────────────────────────────

def print_auth_prompt() -> str:
    """Display a styled sign-in chooser."""
    inner = Table.grid(padding=(0, 2))
    inner.add_column()
    inner.add_row("[bold]You are not signed in.[/bold]")
    inner.add_row("")
    inner.add_row("[bold cyan][1][/bold cyan]  Continue with Google")
    inner.add_row("[bold white][2][/bold white]  Continue as Guest")
    inner.add_row("[dim][3][/dim]  Exit")

    console.print()
    console.print(
        Panel(
            inner,
            title="[bold cyan]Pulse — Sign In[/bold cyan]",
            box=_box_style(),
            border_style="cyan",
            padding=(1, 3),
        )
    )
    console.print()
    try:
        choice = input("  Select an option [1/2/3]: ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = "2"
    if choice not in {"1", "2", "3"}:
        choice = "2"
    return choice


def print_signed_in(display_name: str | None, email: str | None) -> None:
    """Show a success card immediately after Google sign-in."""
    ok = _ok()
    rows: list[str] = []
    if display_name:
        rows.append(f"[bold green]{ok}  {escape(display_name)}[/bold green]")
    if email:
        rows.append(f"[dim]   {escape(email)}[/dim]")
    if not rows:
        rows.append(f"[bold green]{ok}  Signed in[/bold green]")

    inner = Table.grid(padding=(0, 1))
    inner.add_column()
    for row in rows:
        inner.add_row(row)

    console.print(
        Panel(
            inner,
            title="[bold green]Signed In[/bold green]",
            box=_box_style(),
            border_style="green",
            padding=(0, 2),
        )
    )
    console.print()


# ── Provider & Model Selection UI ───────────────────────────────────────────────

def print_provider_selection(
    providers: list[dict[str, Any]], active_provider: str
) -> None:
    """Render interactive provider selection table highlighting active provider."""
    table = Table(
        box=_box_style(),
        show_header=True,
        header_style="bold cyan",
        title="Available AI Providers (Single-Active-Model Agent)",
    )
    table.add_column("#", style="bold yellow", justify="right", width=4)
    table.add_column("Provider", style="bold white", width=18)
    table.add_column("API Key Env Var", style="cyan", width=22)
    table.add_column("Key Status", style="bold", width=18)
    table.add_column("Default Model", style="dim")

    for idx, prov in enumerate(providers, 1):
        key = prov["key"]
        is_active = key.lower() == active_provider.lower()
        active_mark = " [bold cyan](Active)[/bold cyan]" if is_active else ""
        name_str = f"{prov['display_name']}{active_mark}"

        status_str = (
            f"[green]{_ok()} Configured[/green]"
            if prov["configured"]
            else f"[yellow]{_warn()} Missing API Key[/yellow]"
        )

        table.add_row(
            str(idx),
            name_str,
            prov["env_var"],
            status_str,
            prov["default_model"],
        )

    console.print()
    console.print(table)
    console.print()


def print_model_selection(
    provider_name: str,
    models: list[Any],
    default_model: str,
    active_model: str,
) -> None:
    """Render categorized models table with Speed, Context, and Best For metadata."""
    table = Table(
        box=_box_style(),
        show_header=True,
        header_style="bold cyan",
        title=f"Recommended AI Models for {provider_name}",
    )
    table.add_column("#", style="bold yellow", justify="right", width=4)
    table.add_column("Model Name", style="bold white", width=32)
    table.add_column("Speed", style="magenta", width=14)
    table.add_column("Context", style="cyan", width=10)
    table.add_column("Best For", style="white", width=34)
    table.add_column("Status / Tag", style="bold")

    for idx, m in enumerate(models, 1):
        m_name = getattr(m, "name", str(m))
        speed = getattr(m, "speed", "Balanced")
        context = getattr(m, "context_length", "128k")
        best_for = getattr(m, "best_for", "General")
        is_active = m_name.lower() == active_model.lower()
        is_default = m_name.lower() == default_model.lower()

        tags = []
        if is_active:
            tags.append("[bold cyan]Active[/bold cyan]")
        if is_default:
            tags.append("[green]Default[/green]")
        tag_str = ", ".join(tags) if tags else "[dim]Supported[/dim]"

        row_style = "bold cyan" if is_active else ""
        table.add_row(
            str(idx),
            f"*{m_name}" if is_active else m_name,
            speed,
            context,
            best_for,
            tag_str,
            style=row_style,
        )

    console.print()
    console.print(table)
    console.print("[dim]Option [C]: Enter a custom model identifier[/dim]\n")


def print_provider_changed_card(
    provider_display: str, model_name: str, env_var: str, is_configured: bool
) -> None:
    """Card confirming saved provider/model selection."""
    status_str = (
        f"[green]{_ok()} {env_var} Configured[/green]"
        if is_configured
        else f"[yellow]{_warn()} {env_var} Missing (Set in environment or .env)[/yellow]"
    )

    inner = Table.grid(padding=(0, 1))
    inner.add_column(style="bold white", no_wrap=True)
    inner.add_column(style="cyan")
    inner.add_row("Active Provider: ", provider_display)
    inner.add_row("Active Model:    ", model_name)
    inner.add_row("Key Status:      ", status_str)
    inner.add_row("Saved To:        ", ".agent/provider.json")

    console.print(
        Panel(
            inner,
            title="[bold green]AI Provider Configuration Updated[/bold green]",
            box=_box_style(),
            border_style="green",
            padding=(1, 3),
        )
    )
    console.print()


def print_current_model_card(
    provider_display: str,
    model_name: str,
    meta: Any,
    env_var: str,
    is_configured: bool,
) -> None:
    """Render details card for `pulse model current`."""
    status_str = (
        f"[green]{_ok()} Configured ({env_var})[/green]"
        if is_configured
        else f"[red]{_err()} Missing ({env_var})[/red]"
    )

    speed = getattr(meta, "speed", "Balanced") if meta else "Balanced"
    context = getattr(meta, "context_length", "128k") if meta else "128k"
    best_for = getattr(meta, "best_for", "General Assistance") if meta else "General Assistance"
    category = getattr(meta, "category", "Custom") if meta else "Custom"

    inner = Table.grid(padding=(0, 1))
    inner.add_column(style="bold white", no_wrap=True)
    inner.add_column(style="cyan")

    inner.add_row("Active Provider:   ", f"[bold cyan]{provider_display}[/bold cyan]")
    inner.add_row("Active Model:      ", f"[bold white]{model_name}[/bold white]")
    inner.add_row("Category:          ", category)
    inner.add_row("Performance/Speed: ", speed)
    inner.add_row("Context Length:    ", context)
    inner.add_row("Recommended For:   ", best_for)
    inner.add_row("Key Status:        ", status_str)
    inner.add_row("Config File:       ", ".agent/provider.json")

    console.print()
    console.print(
        Panel(
            inner,
            title="[bold cyan]Pulse — Active AI Model Configuration[/bold cyan]",
            box=_box_style(),
            border_style="cyan",
            padding=(1, 3),
        )
    )
    console.print()


def print_all_models_list(
    providers_info: list[dict[str, Any]], active_provider: str, active_model: str
) -> None:
    """Render complete catalog of supported providers and models for `pulse model list`."""
    console.print()
    console.print(_rule("Pulse AI Model Catalog"))
    console.print()

    for p in providers_info:
        is_active_prov = p["key"].lower() == active_provider.lower()
        prov_title = f"[bold cyan]{p['display_name']}[/bold cyan]"
        if is_active_prov:
            prov_title += " [bold green](Active Provider)[/bold green]"

        status_str = (
            f"[green]{_ok()} {p['env_var']} Configured[/green]"
            if p["configured"]
            else f"[yellow]{_warn()} {p['env_var']} Missing[/yellow]"
        )

        table = Table(
            box=_box_style(),
            show_header=True,
            header_style="bold cyan",
            title=f"{prov_title} — {status_str}",
        )
        table.add_column("Model Name", style="bold white", width=32)
        table.add_column("Speed", style="magenta", width=14)
        table.add_column("Context", style="cyan", width=10)
        table.add_column("Best For", style="white", width=34)
        table.add_column("Tag", style="bold")

        for m in p["models"]:
            m_name = getattr(m, "name", str(m))
            speed = getattr(m, "speed", "Balanced")
            context = getattr(m, "context_length", "128k")
            best_for = getattr(m, "best_for", "General")
            is_active = (is_active_prov and m_name.lower() == active_model.lower())
            is_default = m_name.lower() == p["default_model"].lower()

            tags = []
            if is_active:
                tags.append("[bold cyan]Active[/bold cyan]")
            if is_default:
                tags.append("[green]Default[/green]")
            tag_str = ", ".join(tags) if tags else "[dim]Supported[/dim]"

            row_style = "bold cyan" if is_active else ""
            table.add_row(
                f"*{m_name}" if is_active else m_name,
                speed,
                context,
                best_for,
                tag_str,
                style=row_style,
            )

        console.print(table)
        console.print()


# ── Conversation Management UI ─────────────────────────────────────────────────


def _short_id(conv_id: str, length: int = 8) -> str:
    return conv_id[:length]


def _fmt_dt(iso_dt: str) -> str:
    """Format an ISO timestamp to a human-readable short string."""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso_dt)
        # Make it local-aware by stripping tz info for display
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_dt[:16]


def print_chat_list(conversations: list, active_id: str) -> None:
    """Render a table of all conversations."""
    if not conversations:
        console.print()
        console.print(
            _panel(
                "Conversations",
                "No conversations yet.\nStart a new one with [bold cyan]pulse chat new[/bold cyan].",
                style="dim",
            )
        )
        console.print()
        return

    table = Table(
        box=_box_style(),
        show_header=True,
        header_style="bold cyan",
        title="Pulse Conversations",
    )
    table.add_column("#", style="bold yellow", justify="right", width=4)
    table.add_column("ID", style="dim", width=10)
    table.add_column("Title", style="bold white", width=36)
    table.add_column("Turns", style="cyan", justify="right", width=7)
    table.add_column("Last Active", style="dim", width=18)
    table.add_column("Status", style="bold", width=12)

    for idx, conv in enumerate(conversations, 1):
        is_active = conv.id == active_id
        status_str = "[bold cyan]● Active[/bold cyan]" if is_active else "[dim]○ Idle[/dim]"
        row_style = "bold cyan" if is_active else ""
        table.add_row(
            str(idx),
            _short_id(conv.id),
            conv.title,
            str(conv.turn_count),
            _fmt_dt(conv.updated_at),
            status_str,
            style=row_style,
        )

    console.print()
    console.print(table)
    console.print(
        "[dim]Use [bold white]pulse chat switch <ID>[/bold white] to resume a conversation.[/dim]\n"
    )


def print_chat_card(conv: object) -> None:
    """Display a compact card showing the currently active conversation."""
    conv_id = getattr(conv, "id", "")
    title = getattr(conv, "title", "Conversation")
    turn_count = getattr(conv, "turn_count", 0)
    created_at = getattr(conv, "created_at", "")
    updated_at = getattr(conv, "updated_at", "")

    inner = Table.grid(padding=(0, 1))
    inner.add_column(style="dim", no_wrap=True)
    inner.add_column(style="cyan")
    inner.add_row("Conversation:", f"[bold white]{title}[/bold white]")
    inner.add_row("ID:", f"[dim]{_short_id(conv_id)}…[/dim]")
    inner.add_row("Turns:", str(turn_count))
    if created_at:
        inner.add_row("Created:", _fmt_dt(created_at))
    if updated_at and turn_count > 0:
        inner.add_row("Last active:", _fmt_dt(updated_at))

    console.print(
        Panel(
            inner,
            title="[bold cyan]Active Conversation[/bold cyan]",
            box=_box_style(),
            border_style="cyan",
            padding=(0, 2),
        )
    )
    console.print()


def print_chat_created(conv: object) -> None:
    """Success card for `pulse chat new`."""
    conv_id = getattr(conv, "id", "")
    title = getattr(conv, "title", "Conversation")

    inner = Table.grid(padding=(0, 1))
    inner.add_column(style="bold white", no_wrap=True)
    inner.add_column(style="cyan")
    inner.add_row("Title: ", title)
    inner.add_row("ID:    ", f"[dim]{conv_id}[/dim]")
    inner.add_row("Status:", "[bold cyan]Active[/bold cyan]")
    inner.add_row("Tip:   ", "Use [bold]pulse[/bold] to start chatting in this conversation.")

    console.print(
        Panel(
            inner,
            title=f"[bold green]{_ok()}  New Conversation Created[/bold green]",
            box=_box_style(),
            border_style="green",
            padding=(1, 3),
        )
    )
    console.print()


def print_chat_switched(conv: object) -> None:
    """Confirmation card for `pulse chat switch`."""
    conv_id = getattr(conv, "id", "")
    title = getattr(conv, "title", "Conversation")
    turn_count = getattr(conv, "turn_count", 0)
    updated_at = getattr(conv, "updated_at", "")

    inner = Table.grid(padding=(0, 1))
    inner.add_column(style="bold white", no_wrap=True)
    inner.add_column(style="cyan")
    inner.add_row("Title:       ", f"[bold white]{title}[/bold white]")
    inner.add_row("ID:          ", f"[dim]{_short_id(conv_id)}…[/dim]")
    inner.add_row("Turns:       ", str(turn_count))
    if updated_at:
        inner.add_row("Last active: ", _fmt_dt(updated_at))

    console.print(
        Panel(
            inner,
            title=f"[bold cyan]{_ok()}  Switched Conversation[/bold cyan]",
            box=_box_style(),
            border_style="cyan",
            padding=(1, 3),
        )
    )
    console.print()


def print_chat_exported(path: object) -> None:
    """Info card shown after a successful export."""
    console.print(
        Panel(
            f"[green]{_ok()}[/green]  Conversation exported to:\n[bold white]{path}[/bold white]",
            title="[bold green]Export Complete[/bold green]",
            box=_box_style(),
            border_style="green",
            padding=(1, 3),
        )
    )
    console.print()


def print_chat_search_results(results: list, query: str, active_id: str = "") -> None:
    """Render search results table."""
    if not results:
        console.print(
            _panel("Search", f"No conversations found for query: [bold]{query}[/bold]", style="dim")
        )
        console.print()
        return

    table = Table(
        box=_box_style(),
        show_header=True,
        header_style="bold cyan",
        title=f'Search Results — "{query}"',
    )
    table.add_column("#", style="bold yellow", justify="right", width=4)
    table.add_column("ID", style="dim", width=10)
    table.add_column("Title", style="bold white", width=36)
    table.add_column("Turns", style="cyan", justify="right", width=7)
    table.add_column("Last Active", style="dim", width=18)
    table.add_column("Status", style="bold", width=12)

    for idx, conv in enumerate(results, 1):
        is_active = getattr(conv, "id", "") == active_id
        status_str = "[bold cyan]● Active[/bold cyan]" if is_active else "[dim]○ Idle[/dim]"
        row_style = "bold cyan" if is_active else ""
        table.add_row(
            str(idx),
            _short_id(getattr(conv, "id", "")),
            getattr(conv, "title", ""),
            str(getattr(conv, "turn_count", 0)),
            _fmt_dt(getattr(conv, "updated_at", "")),
            status_str,
            style=row_style,
        )

    console.print()
    console.print(table)
    console.print()


# ── Help screen ───────────────────────────────────────────────────────────────

_HELP_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Chat", [
        ("pulse",                "Start interactive chat (restores last conversation)"),
        ('pulse ask "..."',      "Single-shot question"),
    ]),
    ("Conversations", [
        ("pulse chat new [--title T]",  "Start a new conversation"),
        ("pulse chat list",             "List all conversations"),
        ("pulse chat switch ID",        "Resume a previous conversation by ID"),
        ("pulse chat delete ID",        "Permanently delete a conversation"),
        ("pulse chat rename ID TITLE",  "Rename a conversation"),
        ("pulse chat export ID",        "Export conversation to Markdown (default) or JSON"),
        ("pulse chat search QUERY",     "Full-text search across all conversations"),
    ]),
    ("Repository", [
        ("pulse index",          "Build / refresh repository index"),
        ("pulse search QUERY",   "Lexical-semantic file search"),
        ("pulse symbols FILE",   "List imports, classes, and functions"),
    ]),
    ("Git", [
        ("pulse git",                  "Branch status, diff, commit suggestion"),
        ("pulse mutations [--last]",   "Show tracked file mutations"),
        ("pulse rollback",             "Restore latest approved edit"),
        ("pulse edit FILE CONTENT",    "Propose and approve a file change"),
    ]),
    ("Memory", [
        ("pulse memory [--query Q]",   "Inspect or set long-term memory"),
    ]),
    ("Authentication", [
        ("pulse login",           "Sign in with Google OAuth"),
        ("pulse logout",          "Sign out and clear tokens"),
        ("pulse whoami",          "Show signed-in user"),
        ("pulse auth-status",      "Check authentication state"),
    ]),
    ("Verification", [
        ("pulse verify",  "Run the project test suite"),
        ("pulse doctor",  "Check env, config, and provider readiness"),
    ]),
    ("Configuration", [
        ("pulse version",                  "Show the installed Pulse version"),
        ("pulse keys list",                "Show provider key status without values"),
        ("pulse keys set PROVIDER",        "Securely set or rotate a provider key"),
        ("pulse keys remove PROVIDER",     "Remove a workspace provider key"),
        ("pulse model",                    "Interactive AI provider & model manager"),
        ("pulse model current",            "Display active AI provider & model details"),
        ("pulse model list",               "List all supported providers & models"),
        ("pulse model PROVIDER [MODEL]",   "Directly switch AI provider & model"),
        ("pulse status",                   "Show agent configuration"),
        ("pulse serve",                    "Start JSON-RPC WebSocket server"),
        ("pulse tasks [--status S]",       "List workspace tasks"),
        ("pulse sessions",                 "List all sessions"),
    ]),
]


def print_help_screen() -> None:
    """Render the full, grouped, styled help table."""
    outer = Table.grid(padding=(0, 0))
    outer.add_column()

    for group_name, commands in _HELP_GROUPS:
        t = Table(
            box=box.SIMPLE,
            show_header=False,
            padding=(0, 2),
            title=f"[bold cyan]{group_name}[/bold cyan]",
            title_style="bold cyan",
            title_justify="left",
        )
        t.add_column("Command", style="bold white", no_wrap=True)
        t.add_column("Description", style="dim")
        for cmd, desc in commands:
            t.add_row(cmd, desc)
        outer.add_row(t)
        outer.add_row("")

    console.print(
        Panel(
            outer,
            title="[bold cyan]Pulse — Help[/bold cyan]",
            box=_box_style(),
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()


# ── Thinking spinner ──────────────────────────────────────────────────────────

_THINKING_PHASES = [
    "Understanding request...",
    "Searching repository...",
    "Planning...",
    "Generating response...",
]


@contextmanager
def thinking_spinner() -> Generator[None, None, None]:
    """Multi-phase animated spinner while the agent is working."""
    if _is_dumb():
        console.print("Thinking...")
        yield
        return

    stop_event = threading.Event()

    with console.status(
        f"[cyan]{_THINKING_PHASES[0]}[/cyan]", spinner="dots"
    ) as status:

        def _rotate() -> None:
            for phase in itertools.cycle(_THINKING_PHASES):
                if stop_event.wait(timeout=3.0):
                    return
                status.update(f"[cyan]{phase}[/cyan]")

        t = threading.Thread(target=_rotate, daemon=True)
        t.start()
        try:
            yield
        finally:
            stop_event.set()
            t.join(timeout=2)


# ── Session footer & status cards ──────────────────────────────────────────────

@contextmanager
def task_spinner(description: str) -> Generator[None, None, None]:
    """Rich spinner for long-running operations (indexing, tests, git, search)."""
    if _is_dumb():
        console.print(f"{description}...")
        yield
        return

    with console.status(f"[cyan]{description}...[/cyan]", spinner="dots"):
        yield


def print_status_cards(config: Any, provider: Any, runtime: Any = None) -> None:
    """Display rich status cards for Provider, Model, Authentication,
    Repository Indexing, Memory, Sandbox, and Safety Mode.
    """
    auth_str = "Signed Out"
    auth_style = "yellow"
    if runtime and hasattr(runtime, "auth") and runtime.auth.is_authenticated():
        info = runtime.auth.get_current_user_info()
        if info and info[1]:
            auth_str = f"Signed In ({info[1]})"
        else:
            auth_str = f"Signed In (@{runtime.auth.current_user()})"
        auth_style = "green"

    repo_str = "Ready" if (runtime and hasattr(runtime, "repository")) else "Unindexed"
    mem_str = "Active (SQLite)" if (runtime and hasattr(runtime, "memory")) else "Inactive"
    sandbox_str = f"Writes={config.sandbox.allow_writes}, ReadsPerm={config.sandbox.require_permission_for_reads}"
    safety_str = f"Mode={config.mode}, ActionsPerm={config.sandbox.require_permission_for_project_actions}"

    grid = Table(box=_box_style(), show_header=True, header_style="bold cyan", title="Pulse Status Overview")
    grid.add_column("Component", style="bold white", no_wrap=True)
    grid.add_column("Details", style="cyan")
    grid.add_column("State", style="bold")

    api_key_var = getattr(provider, "api_key_env_var", "API Key")
    key_configured = getattr(provider, "is_configured", False)

    grid.add_row(
        "Active Provider",
        escape(str(config.model.provider)),
        f"[green]{_ok()} Configured ({api_key_var})[/green]" if key_configured else f"[red]{_err()} Missing {api_key_var}[/red]",
    )
    grid.add_row("Active Model", escape(str(config.model.name)), f"[dim]Max tokens: {config.model.max_tokens}[/dim]")
    grid.add_row("Authentication", auth_str, f"[{auth_style}]{auth_str}[/{auth_style}]")
    grid.add_row("Repository Indexed", "Code intelligence & semantic search", f"[green]{_ok()} {repo_str}[/green]")
    grid.add_row("Memory Status", "Episodic & preference storage", f"[green]{_ok()} {mem_str}[/green]")
    grid.add_row("Sandbox Status", sandbox_str, f"[cyan]{config.sandbox.workspace_root}[/cyan]")
    grid.add_row("Safety Mode", safety_str, "[blue]Single Model[/blue]")

    console.print()
    console.print(grid)
    console.print()


def _get_git_branch() -> str | None:
    """Return the current Git branch name, or None if unavailable."""
    try:
        result = subprocess.run(  # noqa: PLW1510
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            return branch if branch and branch != "HEAD" else None
    except (OSError, subprocess.CalledProcessError):
        pass
    return None


def print_session_footer(
    provider: str,
    model: str,
    project: str | None = None,
    branch: str | None = None,
    conversation: str | None = None,
) -> None:
    """One-line footer bar shown at the start of interactive sessions."""
    if branch is None:
        branch = _get_git_branch()

    sep = "  [dim]·[/dim]  "
    parts: list[str] = [
        f"[dim]Provider[/dim] [cyan]{provider}[/cyan]",
        f"[dim]Model[/dim] [cyan]{model}[/cyan]",
    ]
    if conversation:
        short_conv = conversation if len(conversation) <= 28 else conversation[:25] + "…"
        parts.append(f"[dim]Chat[/dim] [magenta]{short_conv}[/magenta]")
    if branch:
        parts.append(f"[dim]Branch[/dim] [yellow]{branch}[/yellow]")
    if project:
        short = project if len(project) <= 30 else "..." + project[-27:]
        parts.append(f"[dim]Project[/dim] [white]{short}[/white]")

    console.print(_rule())
    console.print("  " + sep.join(parts))
    console.print()
