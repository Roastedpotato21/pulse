from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from rich.console import Console
from rich.table import Table

from pulse.config import load_agent_config
from pulse.runtime import build_runtime

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(prog="pulse", description="Permissioned single-model project agent.")
    subparsers = parser.add_subparsers(dest="command")

    ask_parser = subparsers.add_parser("ask", help="Ask about the current project.")
    ask_parser.add_argument("question", nargs="+")

    subparsers.add_parser("status", help="Show agent configuration.")
    subparsers.add_parser("doctor", help="Check CLI, uv, provider, and project setup.")

    args = parser.parse_args()
    workspace = Path.cwd()
    config = load_agent_config(workspace)
    runtime = build_runtime(workspace, config)

    try:
        if args.command == "ask":
            runtime.agent.ask(" ".join(args.question), auto_approve_reads=True)
        elif args.command == "status":
            print_status(config, runtime.provider)
        elif args.command == "doctor":
            print_doctor(config, runtime.provider, workspace)
        else:
            run_interactive(runtime.agent)
    finally:
        runtime.audit.print_summary()


def run_interactive(agent: ProjectAgent) -> None:
    print(f"{agent.name} is ready. Type a question, or \"help\" / \"exit\".")
    while True:
        question = input(f"{agent.name}> ").strip()
        if not question:
            continue
        if question in {"exit", "quit"}:
            return
        if question == "help":
            print_help()
            continue
        if question == "status":
            print("Use `pulse status` for full configuration details.")
            continue
        agent.ask(question, auto_approve_reads=True)


def print_help() -> None:
    print(
        """
Pulse CLI

Commands:
  pulse                      Start interactive mode
  pulse ask "..."            Ask about the current project
  pulse status               Show agent configuration
  pulse doctor               Check CLI setup and provider readiness
"""
    )


def print_status(config, provider) -> None:
    table = Table(title=f"{config.agent_name} status", show_header=False)
    table.add_column("Setting", style="cyan")
    table.add_column("Value")
    table.add_row("Mode", config.mode)
    table.add_row("Provider", config.model.provider)
    table.add_row("Model", config.model.name)
    table.add_row("Maximum output tokens", str(config.model.max_tokens))
    table.add_row("Read permission required", str(config.sandbox.require_permission_for_reads))
    table.add_row("Project actions require permission", str(config.sandbox.require_permission_for_project_actions))
    table.add_row("Writes enabled", str(config.sandbox.allow_writes))
    api_key_env_var = getattr(provider, "api_key_env_var", "Provider API key")
    table.add_row(f"{api_key_env_var} present", str(provider.is_configured))
    console.print(table)


def print_doctor(config, provider, workspace: Path) -> None:
    api_key_env_var = getattr(provider, "api_key_env_var", "Provider API key")
    local_pulse = workspace / ".venv" / "Scripts" / "pulse.exe"
    checks = [
        ("Workspace", str(workspace), workspace.exists()),
        ("agent.config.json", str(workspace / "agent.config.json"), (workspace / "agent.config.json").exists()),
        (".env", str(workspace / ".env"), (workspace / ".env").exists()),
        ("Provider key", api_key_env_var, provider.is_configured),
        ("uv command", shutil.which("uv") or "not on PATH", shutil.which("uv") is not None),
        ("pulse command", shutil.which("pulse") or "not on PATH", shutil.which("pulse") is not None),
        ("Local venv pulse", str(local_pulse), local_pulse.exists()),
        ("Single model mode", config.mode, config.mode == "single-model"),
        ("Configured model", config.model.name, bool(config.model.name)),
        ("Maximum output tokens", str(config.model.max_tokens), config.model.max_tokens > 0),
    ]

    table = Table(title="Pulse doctor")
    table.add_column("Check", style="cyan")
    table.add_column("Value")
    table.add_column("State")

    for name, value, ok in checks:
        table.add_row(name, value, "OK" if ok else "Needs attention")

    console.print(table)


if __name__ == "__main__":
    main()
