# Pulse

Day 1 starts with **Pulse**, a small single-model project agent CLI written in Python and managed with `uv`.

## Setup

1. Copy `.env.example` to `.env`.
2. Put your OpenRouter API key in `.env` as `OPENROUTER_API_KEY`.
3. Install and sync the project:

```powershell
uv sync
```

4. Install the local CLI command:

```powershell
uv tool install --editable .
```

5. Check the setup:

```powershell
pulse doctor
```

Now you can call the agent directly from any project terminal:

```powershell
pulse
```

You can also ask one question directly:

```powershell
pulse ask "What files are in this project?"
```

## Current phase

- Single model preset in `agent.config.json`.
- OpenRouter provider configured for `qwen/qwen3-coder`.
- Model responses are capped at 8,192 tokens by default to keep requests within account limits; override with `AGENT_MAX_TOKENS`.
- Read-only project sandbox.
- Permission prompts before reading files.
- Project-affecting actions require permission and writes are disabled for now.
- Every read, edit attempt, and action is shown in the CLI session summary.
- Action logs are written under `.agent/logs/`, which is ignored by git.
- Runtime dependencies are managed by `uv` and currently include `httpx` and `rich`.

## Python module layout

- `pulse.cli`: command line entry point.
- `pulse.agent`: single-agent orchestration and context selection.
- `pulse.provider`: OpenRouter chat-completions provider.
- `pulse.sandbox`: workspace boundary checks and permission gates.
- `pulse.audit`: action/touched-file logging.
- `pulse.config`: project config and `.env` loading.

## Commands

- `pulse`: start an interactive project chat.
- `pulse ask <question>`: ask Pulse about the current project.
- `pulse status`: show the current agent configuration.
- `pulse doctor`: check PATH, uv, project config, and provider readiness.
- `--help`: show command help.

If `pulse` or `uv` is not recognized in an already-open terminal, restart the terminal so the PATH update applies. The installed command locations are:

```powershell
C:\Users\sindh\.local\bin
C:\Users\sindh\AppData\Roaming\Python\Python311\Scripts
```
