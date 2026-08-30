# Pulse Security and Release Audit Report

Date: 2026-08-30
Auditor role: application security, Python release engineering, QA
Workspace: `C:\Users\sindh\OneDrive\Desktop\pulse`
Package: `pulse-coding-agent` 0.1.0
Local audit platform: Windows, Python 3.12.13

## Verdict

Deployment verdict: BLOCKED for public release until external gates complete.

The local Windows security, packaging, and installed-wheel checks are green after remediation. I am not marking the release ready because this audit did not execute the hosted Linux/macOS/Python 3.11/Python 3.13 matrix, live Docker no-skip gates, or live provider e2e gates. Those are material for this package because it executes commands, handles credentials, and exposes local/remote RPC surfaces.

## Executive Summary

I confirmed and fixed multiple release-blocking vulnerabilities across secret handling, logs, subprocess isolation, RPC, remote sandboxing, artifact verification, credential storage, repository indexing, MCP integration, and CLI process boundaries. The most serious confirmed issues were remote execution/archive path risks, unauthenticated local RPC, plaintext/global credential fallback, raw provider/streaming error disclosure, and secret-bearing environment inheritance into child processes.

No real provider credentials were sent to external model providers during this audit. The only provider network exercise used a local fake HTTP server. Controlled synthetic canaries were used only as test fixtures.

I found no known third-party dependency vulnerabilities with `pip-audit`. I found no complete synthetic canary strings in the rebuilt wheel or sdist. A refined scan loaded 2 secret-shaped local `.env` values and found 0 occurrences outside `.env`.

## Confirmed Vulnerabilities Fixed

| ID | Severity | Area | CWE | Status |
| --- | --- | --- | --- | --- |
| F-01 | Critical | Remote sandbox archive/execution IDs/state collision | CWE-22, CWE-59, CWE-639, CWE-400 | Fixed |
| F-02 | Critical | Secret inheritance into verification/git/sandbox subprocesses | CWE-200, CWE-522 | Fixed |
| F-03 | High | Unauthenticated local JSON-RPC and overbroad RPC command surface | CWE-306, CWE-862 | Fixed |
| F-04 | High | Provider/API errors, streaming exceptions, logs, memory, and conversation stores could persist or reflect secrets | CWE-209, CWE-532 | Fixed |
| F-05 | High | OAuth/provider credential storage used plaintext/global fallback patterns | CWE-312, CWE-522 | Fixed |
| F-06 | Medium | `.env`, config, repository index, mutation tracker, and provider config symlink/path issues | CWE-22, CWE-59, CWE-367 | Fixed |
| F-07 | Medium | Release artifact verifier did not reject archive traversal/link/special-file members | CWE-22, CWE-59 | Fixed |
| F-08 | Medium | MCP subprocess/HTTP client boundary allowed exception reflection and non-loopback HTTP endpoints | CWE-200, CWE-918 | Fixed |
| F-09 | Medium | CLI/server entrypoints could expose tracebacks for startup/import validation failures | CWE-209 | Fixed |

Notable code locations:

- Redaction: `src/pulse/sandbox/secrets.py`, `src/pulse/audit.py:23`, `src/pulse/telemetry/logger.py`, `src/pulse/memory.py`, `src/pulse/conversations/manager.py`
- Provider and streaming error safety: `src/pulse/providers/base.py:201`, `src/pulse/streaming.py`
- Credential storage: `src/pulse/auth.py:158`, `src/pulse/provider_keys.py`, `tests/conftest.py`
- Environment/subprocess isolation: `src/pulse/subprocesses.py:30`, `src/pulse/verification.py`, `src/pulse/git.py`, `src/pulse/sandbox/resources.py`, `src/pulse/sandbox/process.py:63`
- RPC hardening: `src/pulse/rpc.py:24`, `src/pulse/rpc.py:161`
- Remote sandbox hardening: `src/pulse/sandbox/remote/models.py`, `src/pulse/sandbox/remote/server.py:54`, `src/pulse/sandbox/remote/worker.py:28`, `src/pulse/sandbox/backend/docker.py`
- MCP hardening: `src/pulse/mcp/client.py:13`
- Release artifact verifier: `scripts/verify_release_artifacts.py:71`
- Operator docs: `README.md:87`, `OPERATIONS.md:29`, `PRIVACY.md:22`

## Plausible Leakage Assessment

Current code after fixes:

- Provider keys set through `pulse keys` are keyring-backed, workspace scoped, hidden-input only, and are not accepted as command arguments.
- OAuth sessions fail closed when the OS credential vault is unavailable; plaintext session fallback is no longer written.
- Audit, telemetry, memory, conversation, provider errors, streaming errors, remote worker results, and remote streamed-output callbacks redact registered and pattern-detected secrets.
- Verification, git, MCP stdio, and sandbox subprocesses use isolated/sanitized environments.
- Repository indexing and mutation snapshots skip symlinks, secret-named files, sensitive suffixes, and oversized files.
- Local RPC requires a strong `PULSE_RPC_TOKEN`, stays loopback-only, and exposes only read-only command names through `pulse.command`.

Residual plausible leakage risks:

- Existing historical local state created before these fixes may already contain sensitive prompts or credentials. I did not delete user history, logs, databases, caches, or backups. The refined scan of current `.env` secret-shaped values found no hits outside `.env`, but that cannot prove absence of all older secrets not currently present in `.env`.
- Docker-dependent tests were skipped on this Windows host because Docker/platform prerequisites were unavailable. Remote sandbox and symlink behavior still requires no-skip Linux/macOS/Docker CI confirmation.
- Live provider behavior was not exercised with real API keys. The local fake-provider test confirmed keys stayed only in the Authorization header for the tested OpenAI-compatible path.
- The old `.audit-wheel-venv` directory could not be fully removed on Windows due access-denied files. I used `.audit-wheel-venv-current` for the clean final wheel smoke test.

## Command Surface Inventory

Installed entry points:

- `pulse`
- `pulse-rpc`
- `pulse-remote`

Primary `pulse` commands:

`version`, `ask`, `model`, `keys`, `chat`, `status`, `doctor`, `mutations`, `edit`, `patch`, `rollback`, `index`, `search`, `symbols`, `verify`, `git`, `memory`, `ci`, `tasks`, `task`, `resume`, `cancel`, `sessions`, `session`, `resume-session`, `login`, `logout`, `whoami`, `auth-status`, `serve`

Nested commands:

- `pulse keys`: `list`, `set`, `rotate`, `remove`
- `pulse chat`: `new`, `list`, `switch`, `delete`, `rename`, `export`, `search`

No `eval` or `evals` public CLI command was present; release evaluation support is script/test based.

## Verification Matrix

| Command | Exit | Result |
| --- | ---: | --- |
| `uv sync --locked` | 1 | Initial sandbox/cache access failure: uv cache access denied |
| `uv sync --locked` escalated | 0 | Resolved 76 packages, checked 68 packages |
| `.venv\Scripts\ruff.exe check src tests scripts` | 0 | All checks passed |
| `.venv\Scripts\mypy.exe` | 0 | Success, no issues found in 5 source files |
| `.venv\Scripts\python.exe -m pytest` | 0 | 436 passed, 17 skipped in 83.89s |
| `.venv\Scripts\pip-audit.exe --progress-spinner off` | 1 | Initial sandbox/network failure reaching PyPI |
| `.venv\Scripts\pip-audit.exe --progress-spinner off` escalated | 0 | No known vulnerabilities found; local package skipped because not on PyPI |
| `uv build` | 1 | Initial sandbox/cache access failure |
| `uv build` escalated | 0 | Built wheel and sdist |
| `.venv\Scripts\python.exe scripts\verify_release_artifacts.py dist --expected-version 0.1.0` | 0 | Wheel and sdist verified |
| `uv venv .audit-wheel-venv-current` escalated | 0 | Created fresh Python 3.12.13 venv |
| `uv pip install --reinstall --python .audit-wheel-venv-current\Scripts\python.exe dist\pulse_coding_agent-0.1.0-py3-none-any.whl` escalated | 0 | Installed 35 packages |
| `.audit-wheel-venv-current\Scripts\pulse.exe --help` | 0 | Installed CLI help renders |
| `.audit-wheel-venv-current\Scripts\pulse-rpc.exe --help` | 0 | Installed RPC help renders |
| `.audit-wheel-venv-current\Scripts\pulse-remote.exe --help` | 0 | Installed remote help renders |
| Installed command help matrix | 0 | 46 help/version paths exited 0 |
| `.audit-wheel-venv-current\Scripts\pulse.exe does-not-exist` | 1 | Clean argparse failure, no traceback |
| `.audit-wheel-venv-current\Scripts\pulse.exe ask` | 1 | Clean missing-argument failure, no traceback |
| `.audit-wheel-venv-current\Scripts\pulse-rpc.exe --host 0.0.0.0` | 1 | Clean generic startup failure, no traceback |
| `.audit-wheel-venv-current\Scripts\pulse-remote.exe --development` | 1 | Clean generic startup failure, no traceback |
| Synthetic canary scan: working tree | 1 | No complete canary strings found outside ignored venv/git dirs |
| Synthetic canary scan: wheel/sdist | 0 | `[]`, no complete canary strings found |
| Refined `.env` leakage scan | 0 | Loaded 2 secret-shaped values, 0 hits outside `.env` |

## Release Blockers Remaining

1. Run hosted CI on the final commit for Linux Python 3.11, Linux Python 3.12, Linux Python 3.13, Windows Python 3.12, and macOS Python 3.12.
2. Run the live Docker/Podman no-skip sandbox/security gate. Local Windows pytest reported 17 skips, including Docker/platform-gated tests.
3. Run live provider e2e with controlled release secrets in CI/protected environment. No real provider credentials were used locally.
4. Review and, if approved, remove or ignore the old generated `.audit-wheel-venv` directory. Windows denied deletion of files inside it during this audit.
5. Review historical local `.agent`, `.pulse`, cache, and backup stores if you suspect pre-fix secrets were previously logged. I did not destructively purge user data.

## Final Assessment

All confirmed in-repo vulnerabilities identified during this audit were fixed and covered by targeted regression tests where practical. No dependency CVEs or current controlled-canary leaks were found. The package is locally clean on Windows/Python 3.12.13, but release should remain blocked until the external CI, Docker, platform, and live-provider gates pass on the final commit.
