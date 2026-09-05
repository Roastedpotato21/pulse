from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import shutil
import sys
import warnings
from pathlib import Path

from rich.table import Table

from pulse import __version__
from pulse.auth import (
    AuthConfigurationError,
    AuthenticationManager,
    AuthError,
    AuthTimeoutError,
    StateMismatchError,
    UserCancelledError,
    get_current_user,
    is_authenticated,
    login,
)
from pulse.ci import github_client
from pulse.ci.runner import CIRunner
from pulse.config import load_agent_config
from pulse.conversations import ConversationManager
from pulse.edits import EditProposal
from pulse.interactive import InteractivePrompt, parse_slash_command
from pulse.provider_keys import ProviderKeyError, ProviderKeyStore
from pulse.providers.discovery import (
    ModelDiscoveryError,
    detect_provider_candidates,
)
from pulse.providers.manager import ProviderManager
from pulse.runtime import build_runtime
from pulse.telemetry import set_correlation_id
from pulse.tool_registry import ToolInvocation

from .cli_ui import (
    print_all_models_list,
    print_auth_prompt,
    print_banner,
    print_chat_created,
    print_chat_exported,
    print_chat_history,
    print_chat_list,
    print_chat_search_results,
    print_chat_switched,
    print_cli_output,
    print_current_model_card,
    print_diff_page,
    print_edit_proposal_summary,
    print_edit_summary,
    print_error,
    print_help_screen,
    print_info,
    print_model_selection,
    print_provider_changed_card,
    print_provider_selection,
    print_session_footer,
    print_signed_in,
    print_status_cards,
    print_success,
    print_warning,
    task_spinner,
    thinking_spinner,
)


def _build_parser() -> argparse.ArgumentParser:
    """Build the public parser shared by batch and interactive modes."""
    parser = argparse.ArgumentParser(prog="pulse", description="Permissioned single-model project agent.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("version", help="Show the installed Pulse version.")

    ask_parser = subparsers.add_parser("ask", help="Ask about the current project.")
    ask_parser.add_argument("question", nargs="+")

    model_parser = subparsers.add_parser("model", help="Interactive AI provider & model manager.")
    model_parser.add_argument("provider", nargs="?", default=None, help="Provider name or subcommand ('list', 'current', gemini, openrouter, openai, anthropic, groq, deepseek)")
    model_parser.add_argument("model", nargs="?", default=None, help="Model identifier")

    keys_parser = subparsers.add_parser(
        "keys", help="Securely manage provider API keys in the OS credential vault."
    )
    keys_subparsers = keys_parser.add_subparsers(dest="keys_command")
    keys_subparsers.add_parser("list", help="Show provider key configuration without values.")
    keys_set = keys_subparsers.add_parser("set", help="Securely prompt for a provider API key.")
    keys_set.add_argument("provider", help="Provider name")
    keys_rotate = keys_subparsers.add_parser(
        "rotate", help="Securely replace a configured provider API key."
    )
    keys_rotate.add_argument("provider", help="Provider name")
    keys_remove = keys_subparsers.add_parser(
        "remove", help="Remove a provider key from secure local storage."
    )
    keys_remove.add_argument("provider", help="Provider name")

    # ── Conversation management ──────────────────────────────────────────────
    chat_parser = subparsers.add_parser("chat", help="Manage conversations.")
    chat_subs = chat_parser.add_subparsers(dest="chat_cmd")

    chat_subs.add_parser("new", help="Start a new conversation.").add_argument(
        "--title", default=None, help="Optional title for the new conversation."
    )
    chat_subs.add_parser("list", help="List all conversations.")

    chat_switch = chat_subs.add_parser("switch", help="Switch to a conversation by ID.")
    chat_switch.add_argument("id", help="Conversation ID (or unique prefix)")

    chat_delete = chat_subs.add_parser("delete", help="Delete a conversation.")
    chat_delete.add_argument("id", help="Conversation ID (or unique prefix)")

    chat_rename = chat_subs.add_parser("rename", help="Rename a conversation.")
    chat_rename.add_argument("id", help="Conversation ID (or unique prefix)")
    chat_rename.add_argument("title", help="New title")

    chat_export = chat_subs.add_parser("export", help="Export a conversation to Markdown or JSON.")
    chat_export.add_argument("id", help="Conversation ID (or unique prefix)")
    chat_export.add_argument("--output", default=None, help="Output file path")
    chat_export.add_argument("--format", dest="fmt", choices=["md", "json"], default="md", help="Export format (md or json)")

    chat_search = chat_subs.add_parser("search", help="Search conversations by title or content.")
    chat_search.add_argument("query", help="Search query")
    # ────────────────────────────────────────────────────────────────────────

    subparsers.add_parser("status", help="Show agent configuration.")
    doctor_parser = subparsers.add_parser(
        "doctor", help="Check CLI, provider, and production deployment readiness."
    )
    doctor_parser.add_argument(
        "--production", action="store_true", help="Run release-blocking production checks."
    )
    doctor_parser.add_argument(
        "--target", choices=("local", "remote"), default="local"
    )
    doctor_parser.add_argument(
        "--json", action="store_true", dest="as_json", help="Emit machine-readable JSON."
    )
    mutations_parser = subparsers.add_parser("mutations", help="Show tracked repository mutations.")
    mutations_parser.add_argument("--last", action="store_true", help="Show only the latest transaction.")
    diff_parser = subparsers.add_parser(
        "diff", help="Review the latest Pulse edit batch without flooding the terminal."
    )
    diff_parser.add_argument(
        "file", nargs="?", default=None, help="File path or number from the diff summary."
    )
    diff_parser.add_argument("--page", type=int, default=1, help="Summary or diff page.")
    edit_parser = subparsers.add_parser("edit", help="Propose a file replacement and request approval.")
    edit_parser.add_argument("file")
    edit_parser.add_argument("content")
    patch_parser = subparsers.add_parser("patch", help="Patch a specific function or class in a file.")
    patch_parser.add_argument("file", help="Path to the target file")
    patch_parser.add_argument("target", help="Name of the function or class to patch")
    patch_parser.add_argument("operation", choices=["insert", "replace", "delete", "rename"], help="Patch operation")
    patch_parser.add_argument("content", nargs="?", default=None, help="Patch content string or path to a file containing the content")
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
    tasks_parser = subparsers.add_parser("tasks", help="List workspace tasks.")
    tasks_parser.add_argument("--status", help="Filter tasks by status (PENDING, RUNNING, COMPLETED, FAILED, PAUSED, CANCELLED)")
    task_detail_parser = subparsers.add_parser("task", help="Display task details.")
    task_detail_parser.add_argument("id", help="Task ID")
    resume_parser = subparsers.add_parser("resume", help="Resume a paused or failed task.")
    resume_parser.add_argument("id", help="Task ID to resume")
    cancel_parser = subparsers.add_parser("cancel", help="Cancel a pending, queued, or running task.")
    cancel_parser.add_argument("id", help="Task ID to cancel")
    cancel_parser.add_argument("--reason", default="Cancelled via CLI", help="Cancellation reason")
    subparsers.add_parser("sessions", help="List all sessions.")
    session_parser = subparsers.add_parser("session", help="Display session details.")
    session_parser.add_argument("id", help="Session ID")
    resume_session_parser = subparsers.add_parser("resume-session", help="Resume an archived or inactive session.")
    resume_session_parser.add_argument("id", help="Session ID to resume")

    # Authentication commands
    subparsers.add_parser(
        "login",
        help="Sign in with Google OAuth 2.0 PKCE flow.",
    )

    subparsers.add_parser("logout", help="Sign out and clear stored tokens.")
    subparsers.add_parser("whoami", help="Show the currently signed-in user.")
    subparsers.add_parser("auth-status", help="Show current authentication status.")

    serve_parser = subparsers.add_parser("serve", help="Start the local JSON-RPC WebSocket server for IDE clients.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    return parser


def _run_main(argv: list[str] | None = None) -> None:
    set_correlation_id()
    parser = _build_parser()
    args = parser.parse_args(argv)
    workspace = Path.cwd()

    if args.command == "version":
        print(f"pulse {__version__}")
        return

    if args.command == "keys":
        _handle_keys_command(workspace, args)
        return

    if args.command in {"login", "logout", "whoami", "auth-status"}:
        auth = AuthenticationManager(workspace)
        if args.command == "login":
            if _handle_login_command():
                _run_provider_onboarding(workspace)
        elif args.command == "logout":
            auth.logout()
            print_success("Signed out and cleared the stored session.")
        elif args.command == "whoami":
            _show_whoami()
        elif auth.is_authenticated():
            user = auth.get_current_user()
            identity = (user.name or user.email) if user else "Authenticated user"
            print_success(f"Signed in as {identity}.")
        else:
            print_warning("Not signed in. Run `pulse login` to authenticate.")
            raise SystemExit(1)
        return

    if args.command == "model":
        _handle_model_command(workspace, args.provider, args.model)
        return

    if args.command == "chat":
        _handle_chat_command(workspace, args)
        return

    config = load_agent_config(workspace)
    if args.command == "serve":
        from pulse.rpc import serve

        asyncio.run(serve(str(workspace), args.host, args.port))
        return

    runtime = build_runtime(workspace)

    try:
        if args.command == "ask":
            with task_spinner("Generating response"):
                runtime.agent.ask(" ".join(args.question), auto_approve_reads=True)
        elif args.command == "status":
            print_status_cards(config, runtime.provider, runtime=runtime)
        elif args.command == "doctor":
            passed = print_doctor(
                config,
                runtime.provider,
                workspace,
                production=args.production,
                target=args.target,
                as_json=args.as_json,
            )
            if not passed:
                raise SystemExit(2)

        elif args.command == "mutations":
            if not is_authenticated():
                print_info("Authentication required. Please login first.")
                return
            print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="mutations", arguments={"last": args.last}))).content, title="Mutations")
        elif args.command == "diff":
            _show_latest_diff(runtime.mutations, args.file, args.page)
        elif args.command == "edit":
            if not is_authenticated():
                print_info("Authentication required. Please login first.")
                return
            result = asyncio.run(runtime.tools.execute(ToolInvocation(name="edit", arguments={"file": args.file, "content": args.content, "reason": "CLI requested edit", "approve": approve_in_cli})))
            print_cli_output(result.content, title="Edit Result")
        elif args.command == "patch":
            if not is_authenticated():
                print_info("Authentication required. Please login first.")
                return
            from pulse.patch import PatchEngine
            content = args.content
            if content and Path(content).is_file():
                content = Path(content).read_text(encoding="utf-8")
            
            patch_engine = PatchEngine(
                edits=runtime.edits,
                safety_manager=runtime.reasoning_engine.safety_manager,
                mutations=runtime.mutations,
                context_manager=runtime.context_manager,
                reasoning_engine=runtime.reasoning_engine,
                task_manager=runtime.task_manager
            )
            
            async def run_patch():
                try:
                    success = await patch_engine.apply_patch(
                        file_path=args.file,
                        target_name=args.target,
                        operation=args.operation,
                        content=content,
                        approve=approve_in_cli
                    )
                    if success:
                        print_info("Patch applied successfully.")
                    else:
                        print_error("Patch rejected or failed safety check.")
                # Intentionally broad to isolate execution boundaries and prevent crashes.
                except Exception as e:  # noqa: BLE001
                    # Intentionally broad at CLI boundary to gracefully report user errors.
                    print_error(f"Patch error: {e}")
            
            asyncio.run(run_patch())
        elif args.command == "rollback":
            if not is_authenticated():
                print_info("Authentication required. Please login first.")
                return
            print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="rollback"))).content, title="Rollback")
        elif args.command == "index":
            with task_spinner("Indexing repository"):
                print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="index"))).content, title="Index")
        elif args.command == "search":
            with task_spinner("Searching repository"):
                print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="search", arguments={"query": args.query}))).content, title="Search")
        elif args.command == "symbols":
            with task_spinner("Parsing symbols"):
                print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="symbols", arguments={"file": args.file}))).content, title="Symbols")
        elif args.command == "verify":
            with task_spinner("Running verification test suite"):
                print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="verify"))).content, title="Verify")
        elif args.command == "git":
            with task_spinner("Analyzing Git repository state"):
                print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="git"))).content, title="Git")
        elif args.command == "memory":
            arguments = {"query": args.query}
            if args.set:
                arguments.update({"preference_key": args.set[0], "preference_value": args.set[1]})
            print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="memory", arguments=arguments))).content, title="Memory")
        elif args.command == "tasks":
            arguments = {"status": args.status} if hasattr(args, "status") else {}
            print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="tasks", arguments=arguments))).content, title="Tasks")
        elif args.command == "task":
            print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="task", arguments={"id": args.id, "action": "show"}))).content, title=f"Task {args.id}")
        elif args.command == "resume":
            print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="task", arguments={"id": args.id, "action": "resume"}))).content, title=f"Resume Task {args.id}")
        elif args.command == "cancel":
            print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="cancel", arguments={"id": args.id, "reason": getattr(args, "reason", "CLI cancellation")}))) .content, title=f"Cancel Task {args.id}")
        elif args.command == "sessions":
            print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="sessions", arguments={}))).content, title="Sessions")
        elif args.command == "session":
            print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="session", arguments={"id": args.id}))).content, title=f"Session {args.id}")
        elif args.command == "resume-session":
            print_cli_output(asyncio.run(runtime.tools.execute(ToolInvocation(name="resume-session", arguments={"id": args.id}))).content, title=f"Resume Session {args.id}")
        elif args.command == "ci":
            if not is_authenticated():
                print_info("Authentication required. Please login first.")
                return
            client = github_client.GitHubClient()
            runner = CIRunner(client, workspace=Path.cwd())
            comment = asyncio.run(runner.run_pr(args.pr))
            print_cli_output(comment, title="CI Comment")
        else:
            _handle_interactive_mode(runtime.auth, runtime.agent, runtime=runtime)
    finally:
        runtime.audit.print_summary()


def _handle_model_command(
    workspace: Path, provider_arg: str | None = None, model_arg: str | None = None
) -> None:
    """Handle interactive model manager, subcommands ('list', 'current'), and direct CLI args."""
    pm = ProviderManager(workspace)

    # Subcommand: pulse model current
    if provider_arg and provider_arg.lower() == "current":
        active_prov, active_mod, warning = pm.validate_active_selection()
        if warning:
            print_warning(warning)
        spec = pm.get_provider_spec(active_prov)
        meta = pm.get_model_metadata(active_prov, active_mod)
        providers_status = {p["key"]: p["configured"] for p in pm.list_providers()}
        print_current_model_card(
            spec.display_name,
            active_mod,
            meta,
            spec.env_var,
            providers_status.get(active_prov, False),
        )
        print_info(f"Selection mode: {pm.get_selection_mode()}")
        return

    # Automatic account-aware selection: pulse model auto [provider]
    if provider_arg and provider_arg.lower() == "auto":
        active_provider, _ = pm.get_active_selection()
        provider = model_arg or active_provider
        try:
            spec = pm.get_provider_spec(provider)
            api_key = ProviderKeyStore(workspace).get(spec.key)
            if not api_key:
                print_error(
                    f"No {spec.display_name} key is configured. Run 'pulse keys set {spec.key}'."
                )
                raise SystemExit(2)
            model = pm.resolve_auto_model(spec.key, api_key)
        except (ValueError, ModelDiscoveryError) as error:
            print_error(str(error))
            raise SystemExit(2) from error
        print_provider_changed_card(spec.display_name, model, spec.env_var, True)
        print_success("Automatic model selection is enabled for this provider.")
        return

    # Subcommand: pulse model list
    if provider_arg and provider_arg.lower() == "list":
        active_prov, active_mod, warning = pm.validate_active_selection()
        if warning:
            print_warning(warning)
        if model_arg:
            try:
                spec = pm.get_provider_spec(model_arg)
                print_model_selection(
                    spec.display_name,
                    spec.available_models,
                    spec.default_model,
                    active_mod if active_prov == spec.key else "",
                )
            except ValueError as error:
                print_error(str(error))
                sys.exit(1)
        else:
            providers = pm.list_providers()
            print_all_models_list(providers, active_prov, active_mod)
        return

    # Direct provider setting: pulse model openrouter [model identifier]
    if provider_arg:
        try:
            spec = pm.get_provider_spec(provider_arg)
        except ValueError as error:
            print_error(str(error))
            sys.exit(1)

        target_model = model_arg or spec.default_model
        saved_prov, saved_mod = pm.save_selection(spec.key, target_model)
        providers_status = {p["key"]: p["configured"] for p in pm.list_providers()}
        print_provider_changed_card(
            spec.display_name,
            saved_mod,
            spec.env_var,
            providers_status.get(saved_prov, False),
        )
        return

    selection = _prompt_provider_and_model(pm)
    if selection is None:
        return
    chosen_key, chosen_model = selection
    spec = pm.get_provider_spec(chosen_key)
    saved_prov, saved_mod = pm.save_selection(chosen_key, chosen_model)
    providers_status = {p["key"]: p["configured"] for p in pm.list_providers()}
    print_provider_changed_card(
        spec.display_name,
        saved_mod,
        spec.env_var,
        providers_status.get(saved_prov, False),
    )


def _prompt_provider_and_model(pm: ProviderManager) -> tuple[str, str] | None:
    """Prompt for one provider/model pair without saving partial selection."""
    active_prov, active_mod, warning = pm.validate_active_selection()
    if warning:
        print_warning(warning)

    providers = pm.list_providers()
    print_provider_selection(providers, active_prov)

    try:
        selection = (
            input("Select provider number (1-6) or key [Enter keeps active]: ")
            .strip()
            .lower()
        )
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled model selection.")
        return

    if not selection:
        chosen_key = active_prov
    elif selection.isdigit():
        idx = int(selection)
        if 1 <= idx <= len(providers):
            chosen_key = providers[idx - 1]["key"]
        else:
            print_error("Invalid provider selection index.")
            return None
    else:
        try:
            chosen_key = pm.get_provider_spec(selection).key
        except ValueError as error:
            print_error(str(error))
            return None

    spec = pm.get_provider_spec(chosen_key)
    print_model_selection(
        spec.display_name,
        spec.available_models,
        spec.default_model,
        active_mod if active_prov == spec.key else "",
    )

    try:
        model_choice = input(
            f"Select model number (1-{len(spec.available_models)}) or enter custom model [Enter keeps default]: "
        ).strip()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled model selection.")
        return

    if not model_choice:
        chosen_model = spec.default_model
    elif model_choice.isdigit():
        m_idx = int(model_choice)
        if 1 <= m_idx <= len(spec.available_models):
            chosen_model = spec.available_models[m_idx - 1].name
        else:
            print_error("Invalid model selection index.")
            return None
    elif model_choice.lower() == "c":
        try:
            chosen_model = input("Enter custom model identifier: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled model selection.")
            return
        if not chosen_model:
            chosen_model = spec.default_model
    else:
        chosen_model = model_choice

    return chosen_key, chosen_model


def _prompt_provider_only(pm: ProviderManager) -> str | None:
    active_provider, _, warning = pm.validate_active_selection()
    if warning:
        print_warning(warning)
    providers = pm.list_providers()
    print_provider_selection(providers, active_provider)
    try:
        choice = input(
            "Select provider number or key [Enter keeps active]: "
        ).strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled provider selection.")
        return None
    if not choice:
        return active_provider
    if choice.isdigit() and 1 <= int(choice) <= len(providers):
        return str(providers[int(choice) - 1]["key"])
    try:
        return pm.get_provider_spec(choice).key
    except ValueError as error:
        print_error(str(error))
        return None


def _handle_login_command() -> bool:
    """Handle `pulse login` experience."""
    if is_authenticated():
        user = get_current_user()
        email = user.email if user else "user"
        print_success(f"Already signed in as {email}.")
        return False

    try:
        user = login()
        if user:
            print_signed_in(user.name, user.email)
            return True
        return False
    except UserCancelledError:
        print_error("Authentication was cancelled in the browser.")
        sys.exit(1)
    except AuthTimeoutError as error:
        print_error(str(error))
        sys.exit(1)
    except StateMismatchError:
        print_error("Authentication state mismatch. Possible security issue.")
        sys.exit(1)
    except AuthConfigurationError:
        print_error("Google sign-in is unavailable in this installation.")
        print_info(
            "Install the latest official pulse-coding-agent release. "
            "Product authentication never requires a workspace .env file."
        )
        sys.exit(1)
    except AuthError as e:
        print_error(f"Authentication failed: {e}")
        sys.exit(1)


def _run_provider_onboarding(workspace: Path) -> bool:
    """Connect a provider key and automatically select an available model."""
    print_info("Authentication complete. Connect your BYOK provider key.")
    manager = ProviderManager(workspace)
    store = ProviderKeyStore(workspace)
    configured = [status for status in store.statuses() if status.configured]

    api_key: str | None
    if configured:
        provider = _prompt_provider_only(manager)
        if provider is None:
            print_warning("Provider setup skipped. Run /keys or /model at any time.")
            return False
        api_key = store.get(provider)
        if api_key:
            print_info(f"Using the existing {provider} credential.")
        else:
            api_key = _read_hidden_provider_key(provider)
    else:
        api_key = _read_hidden_provider_key("provider")
        if not api_key:
            print_warning("Provider setup skipped because no key was entered.")
            return False
        candidates = detect_provider_candidates(api_key)
        if len(candidates) == 1:
            provider = candidates[0]
            print_info(
                f"Detected {manager.get_provider_spec(provider).display_name} from the key format."
            )
        else:
            if candidates:
                print_info(
                    "This key format is shared by multiple providers; choose where it was created."
                )
            else:
                print_info("Choose the provider that issued this key.")
            provider = _prompt_provider_only(manager)
            if provider is None:
                return False

    if not api_key:
        print_warning("Provider setup was not saved because no key was entered.")
        return False

    spec = manager.get_provider_spec(provider)
    try:
        model = manager.resolve_auto_model(provider, api_key, persist=False)
    except ModelDiscoveryError as error:
        if error.status_code in {401, 403}:
            print_error(str(error))
            print_warning("The key and provider selection were not saved.")
            return False
        model = spec.default_model
        print_warning(
            f"Live model discovery is unavailable; using {model} and retrying discovery if it is retired."
        )

    if not store.get(provider):
        try:
            variable = store.set(provider, api_key)
        except ProviderKeyError as error:
            print_error(str(error))
            return False
        print_success(f"Stored {variable} in the OS credential vault.")
    _, saved_model = manager.save_selection(
        provider, model, selection_mode="auto"
    )
    print_provider_changed_card(
        spec.display_name,
        saved_model,
        spec.env_var,
        True,
    )
    print_success("BYOK setup complete. Your next message will use this provider.")
    return True


def _active_provider_has_key(workspace: Path) -> bool:
    manager = ProviderManager(workspace)
    active_provider, _ = manager.get_active_selection()
    return any(
        status.provider == active_provider and status.configured
        for status in ProviderKeyStore(workspace).statuses()
    )


def _read_hidden_provider_key(provider: str) -> str | None:
    """Read a secret only when the terminal can suppress input echo."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(f"Enter the {provider} API key (input hidden): ")
    except getpass.GetPassWarning:
        print_error(
            "Secure hidden input is unavailable in this terminal; the key was not read."
        )
    except (KeyboardInterrupt, EOFError):
        print_warning("API key entry cancelled.")
    return None


def _prompt_and_store_provider_key(
    store: ProviderKeyStore, provider: str, *, rotating: bool
) -> bool:
    value = _read_hidden_provider_key(provider)
    if value is None:
        return False
    try:
        variable = store.rotate(provider, value) if rotating else store.set(provider, value)
    except ProviderKeyError as error:
        print_error(str(error))
        return False
    action = "Rotated" if rotating else "Stored"
    print_success(f"{action} {variable} in the OS credential vault.")
    return True


def _refresh_auto_selection(workspace: Path, provider: str) -> None:
    manager = ProviderManager(workspace)
    selection = manager.get_selection()
    if selection.selection_mode != "auto" or selection.provider != provider:
        return
    api_key = ProviderKeyStore(workspace).get(provider)
    if not api_key:
        return
    try:
        model = manager.resolve_auto_model(provider, api_key)
    except ModelDiscoveryError as error:
        print_warning(f"The key was saved, but automatic model refresh failed: {error}")
        return
    print_info(f"Automatic model selection resolved to {model}.")


def _print_provider_key_statuses(store: ProviderKeyStore) -> None:
    table = Table(title="Provider API keys (secret values are never displayed)")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Provider", style="cyan")
    table.add_column("Environment variable")
    table.add_column("State")
    table.add_column("Source")
    for index, status in enumerate(store.statuses(), 1):
        table.add_row(
            str(index),
            status.provider,
            status.environment_variable,
            "Configured" if status.configured else "Missing",
            status.source,
        )
    print_cli_output(table, title="Provider keys")


def _run_keys_manager(workspace: Path) -> None:
    """Interactive provider-key status, rotation, and removal manager."""
    store = ProviderKeyStore(workspace)
    while True:
        _print_provider_key_statuses(store)
        statuses = store.statuses()
        try:
            choice = input(
                "Select provider number or key to manage [Enter exits]: "
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nKey manager closed.")
            return
        if not choice:
            return
        if choice.isdigit() and 1 <= int(choice) <= len(statuses):
            status = statuses[int(choice) - 1]
        else:
            status = next((item for item in statuses if item.provider == choice), None)
            if status is None:
                print_error("Unknown provider selection.")
                continue

        actions = "[R]otate  [D]elete  [Enter] back" if status.configured else "[S]et  [Enter] back"
        try:
            action = input(f"{status.provider}: {actions}: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nKey manager closed.")
            return
        if action in {"s", "set"} and not status.configured:
            _prompt_and_store_provider_key(store, status.provider, rotating=False)
        elif action in {"r", "rotate"} and status.configured:
            _prompt_and_store_provider_key(store, status.provider, rotating=True)
        elif action in {"d", "delete", "remove"} and status.configured:
            confirm = input(
                f"Remove the stored {status.provider} key? [y/N]: "
            ).strip().lower()
            if confirm in {"y", "yes"}:
                variable, removed, environment_still_set = store.remove(status.provider)
                if removed:
                    print_success(f"Removed {variable} from secure local storage.")
                if environment_still_set:
                    print_warning(
                        f"{variable} remains set by the process environment."
                    )


def _handle_keys_command(workspace: Path, args: object) -> None:
    """Handle provider keys without ever echoing secret values."""
    store = ProviderKeyStore(workspace)
    command = getattr(args, "keys_command", None)
    try:
        if command is None:
            _run_keys_manager(workspace)
            return

        if command == "list":
            _print_provider_key_statuses(store)
            return

        provider = str(getattr(args, "provider", ""))
        if command in {"set", "rotate"}:
            stored = _prompt_and_store_provider_key(
                store,
                provider,
                rotating=command == "rotate",
            )
            if not stored:
                raise SystemExit(2)
            _refresh_auto_selection(workspace, provider)
            return

        if command == "remove":
            variable, removed, environment_still_set = store.remove(provider)
            if removed:
                print_success(f"Removed {variable} from secure local storage.")
            else:
                print_warning(f"{variable} was not present in managed storage.")
            if environment_still_set:
                print_warning(
                    f"{variable} is still configured in the process environment; "
                    "remove it from your shell or secret manager separately."
                )
            return
    except ProviderKeyError as error:
        print_error(str(error))
        raise SystemExit(2) from error


def _show_whoami() -> None:
    """Print current user profile info."""
    user = get_current_user()
    if user:
        if user.name and user.name != user.email:
            print_info(f"{user.name} ({user.email})")
        else:
            print_info(user.email)
    else:
        print_info("Not signed in. Run `pulse login` to sign in.")


def approve_in_cli(proposal: EditProposal) -> bool:
    """Request approval from a compact proposal, with opt-in paginated review."""
    print_edit_proposal_summary(proposal)
    while True:
        answer = input("Apply this edit? [y/N/V review] ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer not in {"v", "view", "review"}:
            return False
        _review_diff_interactively(proposal.file_path, proposal.unified_diff)


def _review_diff_interactively(file_path: str, unified_diff: str) -> None:
    page = 1
    while True:
        page, total_pages = print_diff_page(file_path, unified_diff, page=page)
        if total_pages == 1:
            return
        action = input("Diff [N next/P previous/Q close]: ").strip().lower()
        if action in {"q", "quit", "close", ""}:
            return
        if action in {"n", "next"}:
            page = min(page + 1, total_pages)
        elif action in {"p", "previous", "prev"}:
            page = max(page - 1, 1)


def _show_latest_diff(mutations, selector: str | None, page: int) -> None:
    changes = mutations.last_approved_edit()
    if not changes:
        print_info("No approved Pulse edits are available for review.")
        return
    if selector is None:
        print_edit_summary(changes, page=page, title="Latest Pulse edits")
        return

    selected = None
    if selector.isdigit():
        index = int(selector) - 1
        if 0 <= index < len(changes):
            selected = changes[index]
    else:
        matches = [
            change
            for change in changes
            if str(change.get("file_path", "")) == selector
            or str(change.get("file_path", "")).startswith(selector)
        ]
        if len(matches) == 1:
            selected = matches[0]
    if selected is None:
        print_error(f"No unique edited file matches {selector!r}.")
        return
    print_diff_page(
        str(selected.get("file_path", "unknown file")),
        str(selected.get("unified_diff", "")),
        page=page,
    )


def print_doctor(
    config,
    provider,
    workspace: Path,
    *,
    production: bool = False,
    target: str = "local",
    as_json: bool = False,
) -> bool:
    if production:
        from pulse.production import run_production_checks

        report = run_production_checks(
            workspace,
            config,
            provider_configured=provider.is_configured,
            target=target,
        )
        if as_json:
            print(json.dumps(report.to_dict(), separators=(",", ":")))
            return report.passed

        table = Table(title=f"Pulse production doctor ({target})")
        table.add_column("Check", style="cyan")
        table.add_column("State")
        table.add_column("Detail")
        table.add_column("Remediation")
        for check in report.checks:
            state = "OK" if check.ok else "Warning" if not check.blocking else "BLOCKED"
            table.add_row(check.name, state, check.detail, "" if check.ok else check.remediation)
        print_cli_output(table, title="Production doctor")
        return report.passed

    api_key_env_var = getattr(provider, "api_key_env_var", "Provider API key")
    source_checkout = workspace / "src" / "pulse" / "cli.py"
    running_checkout = (
        not source_checkout.exists()
        or source_checkout.resolve() == Path(__file__).resolve()
    )
    checks = [
        ("Workspace", str(workspace), workspace.exists()),
        ("agent.config.json", str(workspace / "agent.config.json"), (workspace / "agent.config.json").exists()),
        ("Provider key", api_key_env_var, provider.is_configured),
        ("uv command", shutil.which("uv") or "not on PATH", shutil.which("uv") is not None),
        ("pulse command", shutil.which("pulse") or "not on PATH", shutil.which("pulse") is not None),
        ("Loaded Pulse source", str(Path(__file__).resolve()), running_checkout),
        ("Single model mode", config.mode, config.mode == "single-model"),
        ("Configured provider", config.model.provider, bool(config.model.provider)),
        ("Configured model", config.model.name, bool(config.model.name)),
        ("Maximum output tokens", str(config.model.max_tokens), config.model.max_tokens > 0),
    ]

    table = Table(title="Pulse doctor")
    table.add_column("Check", style="cyan")
    table.add_column("Value")
    table.add_column("State")

    for name, value, ok in checks:
        table.add_row(name, value, "OK" if ok else "Needs attention")

    passed = all(ok for _, _, ok in checks)
    if as_json:
        print(
            json.dumps(
                {
                    "target": "development",
                    "passed": passed,
                    "checks": [
                        {"name": name, "value": value, "ok": ok}
                        for name, value, ok in checks
                    ],
                },
                separators=(",", ":"),
            )
        )
    else:
        print_cli_output(table, title="Doctor")
    return passed


def print_mutations(events: list[dict[str, object]]) -> None:
    if not events:
        print_info("No tracked mutations found.")
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
    print_cli_output(table, title="Mutations")


def _handle_chat_command(workspace: Path, args: object) -> None:
    """Dispatch pulse chat subcommands."""
    cm = ConversationManager(workspace)
    chat_cmd = getattr(args, "chat_cmd", None)

    if chat_cmd == "new" or chat_cmd is None:
        title = getattr(args, "title", None)
        conv = cm.create(title=title)
        print_chat_created(conv)
        return

    if chat_cmd == "list":
        conversations = cm.list_all()
        active = cm.get_active()
        active_id = active.id if active else ""
        print_chat_list(conversations, active_id)
        return

    if chat_cmd == "switch":
        conv = _resolve_conversation(cm, args.id)
        if conv is None:
            return
        cm.switch(conv.id)
        print_chat_switched(conv)
        return

    if chat_cmd == "delete":
        conv = _resolve_conversation(cm, args.id)
        if conv is None:
            return
        confirm = input(f'Delete conversation "{conv.title}"? [y/N] ').strip().lower()
        if confirm in {"y", "yes"}:
            cm.delete(conv.id)
            print_info(f'Conversation "{conv.title}" deleted.')
        else:
            print_info("Deletion cancelled.")
        return

    if chat_cmd == "rename":
        conv = _resolve_conversation(cm, args.id)
        if conv is None:
            return
        updated = cm.rename(conv.id, args.title)
        print_info(f'Conversation renamed to "{updated.title}".')
        return

    if chat_cmd == "export":
        conv = _resolve_conversation(cm, args.id)
        if conv is None:
            return
        output_path = Path(args.output) if getattr(args, "output", None) else None
        fmt = getattr(args, "fmt", "md")
        try:
            saved_path = cm.export(conv.id, output_path=output_path, fmt=fmt)
            print_chat_exported(saved_path)
        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception as exc:  # noqa: BLE001
            # Intentionally broad at CLI boundary to gracefully report user errors.
            print_error(f"Export failed: {exc}")
        return

    if chat_cmd == "search":
        results = cm.search(args.query)
        active = cm.get_active()
        active_id = active.id if active else ""
        print_chat_search_results(results, args.query, active_id)
        return

    # Unknown subcommand — show list
    conversations = cm.list_all()
    active = cm.get_active()
    active_id = active.id if active else ""
    print_chat_list(conversations, active_id)


def _resolve_conversation(cm: ConversationManager, id_prefix: str):
    """Resolve a conversation by full ID or unique prefix. Returns None on failure."""
    all_convs = cm.list_all()
    if id_prefix.isdigit():
        active = cm.get_active()
        history = [
            conv
            for conv in all_convs
            if conv.turn_count > 0 and (active is None or conv.id != active.id)
        ]
        index = int(id_prefix) - 1
        if 0 <= index < len(history):
            return history[index]
        print_error(f"No previous conversation exists at position {id_prefix}.")
        return None
    matches = [c for c in all_convs if c.id == id_prefix or c.id.startswith(id_prefix)]
    if not matches:
        print_error(f"No conversation found matching ID prefix: {id_prefix!r}")
        return None
    if len(matches) > 1:
        print_error(
            f"Ambiguous prefix {id_prefix!r} matches {len(matches)} conversations. "
            "Please use a longer prefix."
        )
        return None
    return matches[0]


def _handle_interactive_mode(auth, agent, runtime=None) -> None:
    """Handle the responsive interactive shell with conversation tracking."""
    del auth  # Authentication state is read from the shared workspace store.
    workspace = Path.cwd()
    cm = ConversationManager(workspace)
    prompt = InteractivePrompt(workspace, _build_parser())
    _warn_if_checkout_is_not_active(workspace)

    # A plain `pulse` launch always begins a fresh chat. Previous chats remain
    # available explicitly through `/chat switch <ID>`.
    previous_conversations = cm.list_all()
    active_conv = cm.create()
    is_first_message = True
    resume_history: list[tuple[str, str]] = []

    print_banner()
    if not is_authenticated():
        auth_choice = print_auth_prompt()
        if auth_choice == "1":
            if _handle_login_command():
                _run_provider_onboarding(workspace)
                runtime = build_runtime(workspace)
                agent = runtime.agent
        elif auth_choice == "3":
            return
        else:
            print_info("Continuing unauthenticated.")
    else:
        user = get_current_user()
        if user:
            print_signed_in(user.name, user.email)
        if not _active_provider_has_key(workspace):
            print_warning("The active model provider has no API key configured.")
            answer = input("Configure BYOK now? [Y/n] ").strip().lower()
            if answer not in {"n", "no"} and _run_provider_onboarding(workspace):
                runtime = build_runtime(workspace)
                agent = runtime.agent

    print_chat_history(previous_conversations)
    print_info("Type / to open the command menu, or /help to see every command.")

    while True:
        try:
            user_input = prompt.read()
            if not user_input:
                continue
            normalized = user_input.lower()
            if normalized in {"exit", "quit", "/exit", "/quit"}:
                break
            if normalized in {"help", "?", "/help", "/?"}:
                print_help_screen(interactive=True)
                continue
            if normalized == "/clear":
                print("\033[2J\033[H", end="")
                continue
            if user_input.startswith("/"):
                try:
                    command_args = parse_slash_command(user_input)
                except ValueError as error:
                    print_error(str(error))
                    continue
                if not command_args:
                    continue
                try:
                    main(command_args)
                except KeyboardInterrupt:
                    print_warning("Command cancelled.")
                except SystemExit as error:
                    # argparse and command handlers use SystemExit for ordinary
                    # user errors; a REPL command must never terminate the shell.
                    if error.code not in {None, 0}:
                        print_warning(f"Command finished with exit code {error.code}.")

                refreshed = cm.get_active()
                if command_args[0] == "chat" and refreshed is None:
                    refreshed = cm.create()
                if refreshed is not None:
                    changed_conversation = refreshed.id != active_conv.id
                    active_conv = refreshed
                    is_first_message = active_conv.turn_count == 0
                    if changed_conversation:
                        runtime = build_runtime(workspace)
                        agent = runtime.agent
                        resume_history = [
                            (turn.role, turn.content)
                            for turn in cm.get_turns(active_conv.id)
                        ]

                # Model and key changes must take effect on the very next prompt.
                if command_args[0] in {"model", "keys"}:
                    runtime = build_runtime(workspace)
                    agent = runtime.agent
                continue

            interrupted = False
            output = ""
            try:
                if agent.should_create_workspace_files(user_input):
                    with thinking_spinner():
                        files = agent.plan_workspace_files(
                            user_input,
                            conversation_history=resume_history,
                        )
                    applied = agent.apply_workspace_files(files, approve_in_cli)
                    if applied:
                        output = f"Edited {len(applied)} file{'s' if len(applied) != 1 else ''}."
                        print_edit_summary(applied)
                    else:
                        output = "No files were created because the proposed edits were declined."
                        print_warning(output)
                else:
                    with thinking_spinner():
                        output = (
                            agent.ask(
                                user_input,
                                auto_approve_reads=True,
                                conversation_id=active_conv.id,
                                conversation_history=resume_history,
                            )
                            or ""
                        )
                resume_history = []
            except KeyboardInterrupt:
                interrupted = True
            except (RuntimeError, TypeError, ValueError) as error:
                print_error(str(error))

            if interrupted:
                print_warning("Request cancelled. Your session is still active.")
                continue

            # Auto-title on first message in a fresh conversation
            if is_first_message:
                active_conv = cm.auto_title(active_conv.id, user_input)
                is_first_message = False

            # Record turns
            cm.add_turn(active_conv.id, "user", user_input)
            if output.strip():
                cm.add_turn(active_conv.id, "assistant", output.strip())

            print_session_footer(
                provider=runtime.provider.config.provider if runtime else "pulse",
                model=runtime.provider.config.name if runtime else "default",
                conversation=active_conv.title,
            )
        except (KeyboardInterrupt, EOFError):
            print("\nExiting Pulse REPL.")
            break


def _warn_if_checkout_is_not_active(workspace: Path) -> bool:
    """Warn when a global executable shadows the source checkout being tested."""
    checkout_cli = workspace / "src" / "pulse" / "cli.py"
    if not checkout_cli.exists() or checkout_cli.resolve() == Path(__file__).resolve():
        return False
    local_executable = workspace / ".venv" / "Scripts" / "pulse.exe"
    print_warning(
        "This pulse command is not running the current source checkout. "
        f"Loaded {Path(__file__).resolve()}. Use `{local_executable}` or `uv run pulse` "
        "to test local changes."
    )
    return True


def main(argv: list[str] | None = None) -> None:
    try:
        _run_main(argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print_warning("Command cancelled.")
        raise SystemExit(130) from None
    except AuthError as error:
        print_error(f"Authentication failed: {error}")
        raise SystemExit(1) from None
    except (EOFError, OSError, RuntimeError, ValueError, json.JSONDecodeError):
        if os.environ.get("PULSE_DEBUG") == "1":
            raise
        print_error("Pulse could not complete the request. Check configuration and inputs.")
        raise SystemExit(1) from None
    except Exception:
        if os.environ.get("PULSE_DEBUG") == "1":
            raise
        print_error("Pulse could not complete the request. Check configuration and inputs.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
