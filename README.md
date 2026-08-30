# Pulse

Pulse is a permissioned coding-agent CLI for working on software repositories.
It combines repository-aware planning, durable task state, explicit mutation
controls, sandboxed command execution, provider routing, audit trails, and cost
tracking in one Python package.

> **Public beta — 0.1.0:** Pulse is supported for controlled, local,
> single-user use. The remote worker and VS Code extension are included for
> evaluation and development, but they are not supported as multi-tenant or
> unattended production services.

## Requirements

- Python 3.11, 3.12, or 3.13
- An API key for the model provider you choose
- Docker or Podman for local isolated execution, or access to a configured
  Pulse remote worker

## Install

Install the first beta from PyPI:

```bash
uv tool install pulse-coding-agent==0.1.0
```

Alternatively, use `pipx install pulse-coding-agent==0.1.0` or install it in a
dedicated virtual environment with `pip`.

## Quick start

Run these commands from the repository you want Pulse to work on:

```bash
pulse login
pulse doctor --production --target local
pulse ask "Explain this repository and identify the highest-risk missing test"
```

After Google login, Pulse guides provider selection, model selection, and hidden
BYOK entry. New provider keys are stored in the native OS credential vault.
Credentials can also be supplied through environment variables such as `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or `OPENROUTER_API_KEY`. Do not commit a
populated `.env` file.

To run commands without installing Docker on the client workstation, configure
a remote worker:

```text
PULSE_REMOTE_URL=wss://sandbox.example.com
PULSE_REMOTE_TOKEN=<random token of at least 32 characters>
PULSE_TLS_CERT=/absolute/path/to/client.crt
PULSE_TLS_KEY=/absolute/path/to/client.key
PULSE_TLS_CA=/absolute/path/to/ca.crt
```

When those variables are set, Pulse tries the remote sandbox first and uses a
local Docker/Podman engine only as a secure fallback. The remote worker host
still requires Docker or Podman; execution never silently falls back to the
client host.

Before allowing mutations or shell execution, review the proposed operation and
its permission prompt. Treat model output, tool arguments, and repository
content as untrusted input.

## What the beta includes

- Repository indexing and context-aware conversations
- Durable plans, tasks, sessions, episodic memory, and recovery state
- Permission-gated file mutations with transactional rollback
- Host, Docker, Podman, and authenticated remote sandbox backends
- Filesystem, network, resource, timeout, and secret-redaction policies
- Multi-provider model routing and usage/cost accounting
- Loopback JSON-RPC integration through `pulse-rpc` or `pulse serve`
- Production preflight checks, structured audit events, and correlation IDs

The package installs three entry points:

```text
pulse          Interactive CLI
pulse-rpc      Loopback JSON-RPC server
pulse-remote   Evaluation-only remote sandbox worker
```

Use `pulse --help` for the current command surface and command-specific help.
`pulse-rpc` and `pulse serve` require a strong `PULSE_RPC_TOKEN` bearer secret
and must stay bound to loopback.

### Interactive shell

Run `pulse` without arguments for the responsive project shell. Natural-language
input goes to the agent; prefix any regular CLI command with `/` to run it in the
same session, such as `/status`, `/keys list`, or `/chat switch ID`. Completions
appear as you type and are always generated from the public CLI parser.

- `Tab` or `Ctrl-Space`: open/accept command completion
- `Up`/`Down`: navigate history; `Ctrl-R`: search history
- `Alt-Enter`: insert a newline; `Enter`: submit
- `Ctrl-C`: clear the current input; on an empty prompt, exit
- `/help`, `/clear`, and `/exit`: shell controls

History is stored locally in the Git-ignored `.pulse/history` file.

### Account and provider keys

```bash
pulse version
pulse login
pulse auth-status
pulse whoami
pulse logout
pulse keys
pulse keys list
pulse keys set openai
pulse keys rotate openai
pulse keys remove openai
```

`pulse keys` opens the status and rotation manager. Set and rotate operations
prompt with hidden input; provider keys are never accepted as command-line
arguments or printed back to the terminal. New keys are stored in a
workspace-scoped entry in the native OS credential vault. Existing `.env` keys
remain readable for compatibility and are removed for that provider after a
successful vault-backed set or rotation. Environment variables and external
secret managers remain supported as fallback sources.

## Supported boundary

The 0.1.0 beta supports the local CLI and loopback RPC server for one trusted
user on one workstation. Pulse does not claim a security boundary between
mutually untrusted tenants. Do not expose `pulse-rpc` or `pulse-remote` directly
to the public internet.

The remote worker requires authenticated transport, durable configuration, and
container isolation. It remains evaluation-only until tenant isolation,
centralized observability, load testing, disaster-recovery exercises, and an
independent security review are complete. The VS Code extension is source-only
and is not part of the Python distribution.

Pulse sends prompts and selected repository context to the configured model
provider. Data handling therefore also depends on that provider's terms and
settings. Pulse itself does not include product analytics or telemetry export
in this beta.

## Development

Clone the repository and create the locked development environment:

```bash
uv sync --locked
uv run pulse --help
```

Run the local quality gates:

```bash
uv run ruff check src tests scripts
uv run mypy
uv run pytest tests/ -k "not sandbox" --cov=pulse --cov-report=term-missing --cov-fail-under=54
uv build
uv run python scripts/verify_release_artifacts.py dist --expected-version 0.1.0
```

Docker security tests require a working Docker daemon and are enforced by the
hosted release workflow. See [OPERATIONS.md](OPERATIONS.md) for release and
rollback procedures.

## Documentation

- [Architecture](ARCHITECTURE.md)
- [Operations and releases](OPERATIONS.md)
- [Security policy](SECURITY.md)
- [Sandbox security boundary](src/pulse/sandbox/SECURITY.md)
- [Privacy policy](PRIVACY.md)
- [Changelog](CHANGELOG.md)

To report a vulnerability, use GitHub private vulnerability reporting instead
of a public issue. General defects and beta feedback belong in the
[issue tracker](https://github.com/Roastedpotato21/pulse/issues).
