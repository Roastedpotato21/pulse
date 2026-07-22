from __future__ import annotations

import argparse
import asyncio
import shutil
from pathlib import Path

from rich.console import Console
from rich.table import Table

from pulse.config import load_agent_config
from pulse.runtime import build_runtime
from pulse.edits import EditProposal
from pulse.tool_registry import ToolInvocation
import pulse.ci.github_client as github_client
from pulse.ci.runner import CIRunner

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(prog="pulse", description="Permissioned single-model project agent.")
    subparsers = parser.add_subparsers(dest="command")

    ask_parser = subparsers.add_parser("ask", help="Ask about the current project.")
    ask_parser.add_argument("question", nargs="+")

    subparsers.add_parser("status", help="Show agent configuration.")
    subparsers.add_parser("doctor", help="Check CLI, uv, provider, and project setup.")
    mutations_parser = subparsers.add_parser("mutations", help="Show tracked repository mutations.")
    mutations_parser.add_argument("--last", action="store_true", help="Show only the latest transaction.")
    edit_parser = subparsers.add_parser("edit", help="Propose a file replacement and request approval.")
    edit_parser.add_argument("file")
    edit_parser.add_argument("content")
    subparsers.add_parser("rollback", help="Rollback the latest approved edit.")
    subparsers.add_parser("index", help="Incrementally index the repository.")
    search_parser = subparsers.add_parser("search", help="Search indexed repository files.")
    search_parser.add_argument("query")
    symbols_parser = subparsers.add_parser("symbols", help="Show indexed symbols from a file.")
    symbols_parser.add_argument("file")
    subparsers.add_parser("verify", help="Run the detected project test suite.")
    subparsers.add_parser("git", help="Show Git status, diff analysis, and a commit suggestion.")
    memory_parser = subparsers.add_parser("memory", help="Inspect long-term memory or save a preference.")
    memory_parser.add_argument("--query", default="", help="Search remembered context.")
    memory_parser.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"), help="Save a user preference.")
    ci_parser = subparsers.add_parser("ci", help="Run CI for a pull request.")
    ci_parser.add_argument("--pr", type=int, required=True, help="Pull request number to process.")
    serve_parser = subparsers.add_parser("serve", help="Start the local JSON-RPC WebSocket server for IDE clients.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    args = parser.parse_args()
    workspace = Path.cwd()
    config = load_agent_config(workspace)
    if args.command == "serve":
        from pulse.rpc import serve

        asyncio.run(serve(str(workspace), args.host, args.port))
        return
    runtime = build_runtime(workspace, config)

    try:
        if args.command == "ask":
            runtime.agent.ask(" ".join(args.question), auto_approve_reads=True)
        elif args.command == "status":
            print(asyncio.run(runtime.tools.execute(ToolInvocation(name="status"))).content)
        elif args.command == "doctor":
            print(asyncio.run(runtime.tools.execute(ToolInvocation(name="doctor"))).content)
        elif args.command == "mutations":
            print(asyncio.run(runtime.tools.execute(ToolInvocation(name="mutations", arguments={"last": args.last}))).content)
        elif args.command == "edit":
            result = asyncio.run(runtime.tools.execute(ToolInvocation(name="edit", arguments={"file": args.file, "content": args.content, "reason": "CLI requested edit", "approve": approve_in_cli})))
            print(result.content)
        elif args.command == "rollback":
            print(asyncio.run(runtime.tools.execute(ToolInvocation(name="rollback"))).content)
        elif args.command == "index":
            print(asyncio.run(runtime.tools.execute(ToolInvocation(name="index"))).content)
        elif args.command == "search":
            print(asyncio.run(runtime.tools.execute(ToolInvocation(name="search", arguments={"query": args.query}))).content)
        elif args.command == "symbols":
            print(asyncio.run(runtime.tools.execute(ToolInvocation(name="symbols", arguments={"file": args.file}))).content)
        elif args.command == "verify":
            print(asyncio.run(runtime.tools.execute(ToolInvocation(name="verify"))).content)
        elif args.command == "git":
            print(asyncio.run(runtime.tools.execute(ToolInvocation(name="git"))).content)
        elif args.command == "memory":
            arguments = {"query": args.query}
            if args.set:
                arguments.update({"preference_key": args.set[0], "preference_value": args.set[1]})
            print(asyncio.run(runtime.tools.execute(ToolInvocation(name="memory", arguments=arguments))).content)
        elif args.command == "ci":
            client = github_client.GitHubClient()
            runner = CIRunner(client, workspace=Path.cwd())
            comment = asyncio.run(runner.run_pr(args.pr))
            print(comment)
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
  pulse edit FILE CONTENT    Show a proposed diff and ask before writing
  pulse rollback             Restore the last approved edit
  pulse index                Incrementally index the repository
  pulse search QUERY         Search indexed files by name and symbols
  pulse symbols FILE         Show a file's indexed symbols
  pulse verify               Run the detected project test suite
  pulse git                  Show Git state and a commit suggestion
  pulse memory [--query Q]   Inspect memory or save a preference with --set
  pulse serve                Start the local IDE JSON-RPC server
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


async def approve_in_cli(proposal: EditProposal) -> bool:
    console.print(f"Proposed edit: {proposal.file_path}\n{proposal.unified_diff or '(no changes)'}")
    answer = input("Apply this edit? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


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


def print_mutations(events: list[dict[str, object]]) -> None:
    if not events:
        console.print("No tracked mutations found.")
        return

    table = Table(title="Pulse mutations")
    table.add_column("Transaction", style="cyan")
    table.add_column("Time")
    table.add_column("Action")
    table.add_column("File")
    table.add_column("Command")
    for event in events:
        table.add_row(
            str(event.get("transaction_id", ""))[:8],
            str(event.get("timestamp", "")),
            str(event.get("action", "")),
            str(event.get("file_path", "")),
            str(event.get("command") or ""),
        )
    console.print(table)


if __name__ == "__main__":
    main()
