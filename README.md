# Pulse

**Pulse** is an autonomous, multi-capability project agent CLI written in Python and managed with `uv`. It combines repository intelligence, hierarchical planning, safety-gated execution, episodic memory, cost tracking, multi-provider AI support, and VS Code IDE integration.

## Setup

1. Copy `.env.example` to `.env`.
2. Add your API keys (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, or `DEEPSEEK_API_KEY`).
3. Install and sync:

```powershell
uv sync
```

4. Install the local CLI:

```powershell
uv tool install --editable .
```

5. Select your AI provider and model using the interactive model manager:

```powershell
pulse model
```

6. Check the setup:

```powershell
pulse doctor
```

Ask your first question:

```powershell
pulse ask "What files are in this project?"
```

---

## AI Provider & Model Management

Pulse supports **6 AI Providers** as a single-active-model agent with rich model metadata (Speed, Context Length, Best For recommendations):

| Provider | Environment Variable | Recommended Models | Speed | Context | Best For |
|---|---|---|---|---|---|
| **Google Gemini** | `GEMINI_API_KEY` | `gemini-2.0-flash`<br>`gemini-1.5-pro` | Fast<br>High Quality | 1M<br>2M | General & Fast Coding<br>Reasoning & Deep Analysis |
| **OpenRouter** | `OPENROUTER_API_KEY` | `qwen/qwen3-coder`<br>`anthropic/claude-3.5-sonnet`<br>`deepseek/deepseek-r1` | Balanced<br>High Quality<br>High Quality | 128k<br>200k<br>164k | Advanced Coding & Refactoring<br>Architecture & Technical Writing<br>Complex Reasoning & STEM |
| **OpenAI** | `OPENAI_API_KEY` | `gpt-4o`<br>`gpt-4o-mini`<br>`o1`<br>`o3-mini` | High Quality<br>Fast<br>High Quality<br>Fast | 128k<br>128k<br>200k<br>200k | Multimodal, Architecture & Coding<br>Lightweight Code & Fast Chat<br>STEM & Complex Reasoning<br>Fast Technical Reasoning |
| **Anthropic** | `ANTHROPIC_API_KEY` | `claude-3-5-sonnet-20241022`<br>`claude-3-5-haiku-20241022` | High Quality<br>Fast | 200k<br>200k | State-of-the-Art Coding & Design<br>Rapid Refactoring & Lightweight Tasks |
| **Groq** | `GROQ_API_KEY` | `llama-3.3-70b-versatile`<br>`llama-3.1-8b-instant` | Ultra-Fast<br>Ultra-Fast | 128k<br>128k | General & Fast Coding<br>Instant Search & Micro-Edits |
| **DeepSeek** | `DEEPSEEK_API_KEY` | `deepseek-chat`<br>`deepseek-reasoner` | Balanced<br>High Quality | 64k<br>64k | General Assistant & Coding (V3)<br>Chain-of-Thought Reasoning (R1) |

### Model Management Commands

- **Interactive Model Manager**: `pulse model` — Opens interactive provider & model selection with Speed, Context, and Best For metadata.
- **Show Active Configuration**: `pulse model current` — Displays active provider, model, context length, speed, key status, and config path.
- **List All Providers & Models**: `pulse model list` — Displays complete catalog of all supported providers and models.
- **Direct Switch**: `pulse model anthropic claude-3-5-sonnet-20241022` or `pulse model groq llama-3.3-70b-versatile`.
- **Status Check**: `pulse status` — Displays overall agent status.

Your selected active provider and model are permanently stored in `.agent/provider.json`:
```json
{
  "provider": "anthropic",
  "model": "claude-3-5-sonnet-20241022"
}
```

---

## Python module layout

| Module | Purpose |
|---|---|
| `pulse.auth` | `AuthenticationManager` - Google OAuth 2.0 PKCE, token refresh, session persistence, keyring store |
| `pulse.cli` | Command line entry point |
| `pulse.agent` | Single-agent orchestration and context selection |
| `pulse.orchestration` | `AgentOrchestrator` - intent routing via Repository Intelligence |
| `pulse.safety` | `SafetyManager`, `RiskLevel` - LOW / MEDIUM / HIGH action risk assessment |
| `pulse.planner.execution_loop` | `AutonomousLoop` - multi-turn tool execution with checkpointing |
| `pulse.planner.dag_planner` | `DAGPlanner` - hierarchical DAG decomposition of multi-file features |
| `pulse.providers` | `ProviderManager`, `BaseProvider`, Gemini, OpenRouter, OpenAI, Anthropic, Groq, DeepSeek providers |
| `pulse.providers.failover` | `FailoverProvider` - transparent secondary provider fallback |
| `pulse.telemetry` | `CostTracker`, `TelemetryLogger` - token/cost budgets and structured metrics |
| `pulse.refactor.impact_analyzer` | `ASTImpactAnalyzer` - AST cross-reference symbol impact analysis |
| `pulse.episodic` | `EpisodicMemory` - SQLite execution trace storage |
| `pulse.rule_synthesizer` | `RuleSynthesizer` - auto-generated `.agent/rules` from repeated error patterns |
| `pulse.provider` | Provider re-exports and `ProviderFactory` backward compatibility |
| `pulse.repository` | Async incremental repository index and semantic search |
| `pulse.verification` | Async test-runner detection, diagnostics, and repair/retry |
| `pulse.git` | Git status, diff analysis, and commit suggestions |
| `pulse.memory` | SQLite long-term context, preferences, and task memory |
| `pulse.multi_agent` | Async Planner -> Coder -> Reviewer -> Tester role pipeline |
| `pulse.mutations` | File mutation tracking, snapshots, diffs, and rollback |
| `pulse.rpc` | JSON-RPC 2.0 WebSocket adapter for IDE clients |
| `pulse.audit` | Session action log |
| `pulse.sandbox` | Workspace permission boundary with local Docker and Remote Sandbox execution (`pulse-remote`) |
| `pulse.config` | Project config, `.agent/provider.json` and `.env` loading |
| `pulse.tool_registry` | Async Tool interface, permission gates, and concurrent dispatch |
| `pulse.tools` | Built-in tools: status, doctor, edit, rollback, mutations, verify, git |
| `pulse.conversations` | `ConversationManager` — SQLite-backed multi-conversation manager with turn history, search, and export |


---

## Commands

### Conversation Management

Pulse supports **multiple named conversations** with full history, search, and export. Each conversation is persisted in SQLite and the last active one is automatically restored when you start `pulse`.

```powershell
# Start a new conversation (auto-titled from your first message)
pulse chat new
pulse chat new --title "Refactoring Sprint"

# List all conversations (highlights the active one)
pulse chat list

# Resume a previous conversation by ID (or prefix)
pulse chat switch <ID>

# Rename a conversation
pulse chat rename <ID> "New Title"

# Delete a conversation
pulse chat delete <ID>

# Export to Markdown (default) or JSON
pulse chat export <ID>
pulse chat export <ID> --format json
pulse chat export <ID> --output ./my-chat.md

# Search conversations by title or message content
pulse chat search "authentication"
```

The active conversation name is shown in the interactive prompt and footer:

```
pulse [Refactoring Sprint]> What does this function do?
```

Conversations are stored in `.agent/conversations.sqlite3`.

### All Commands

```
pulse                               Interactive project chat (restores last conversation)
pulse ask <question>                Single-shot project question
pulse chat new [--title T]          Start a new conversation
pulse chat list                     List all conversations with status
pulse chat switch <ID>              Resume a conversation by ID or prefix
pulse chat delete <ID>              Permanently delete a conversation
pulse chat rename <ID> TITLE        Rename a conversation
pulse chat export <ID> [--format]   Export conversation to Markdown or JSON
pulse chat search QUERY             Full-text search across all conversations
pulse model                         Interactive AI provider & model manager
pulse model current                 Display active AI provider & model details
pulse model list                    List all supported AI providers & models
pulse model [PROVIDER] [MODEL]      Directly select AI provider and model
pulse status                        Show agent configuration & active provider/model
pulse doctor                        Check env, config, and provider readiness
pulse login                         Sign in with Google OAuth (Continue with Google)
pulse logout                        Sign out and clear stored tokens
pulse whoami                        Show the currently signed-in user (name, email)
pulse google-login                  Alias for `pulse login` - opens Google OAuth in browser
pulse auth-status                   Detailed sign-in status

pulse register <user> <pass>        Register a local account
pulse edit <file> <content>         Propose a diff, approve or discard
pulse rollback                      Restore latest approved edit snapshot
pulse mutations [--last]            Inspect tracked file mutations
pulse index                         Build / refresh repository index
pulse search <query>                Lexical-semantic file search
pulse symbols <file>                List imports, classes, functions for a file
pulse verify                        Run project test suite
pulse git                           Branch status, diff, commit suggestion
pulse memory [--query <text>]       Inspect or set long-term memory
pulse serve                         Start loopback JSON-RPC WebSocket server
```

---

## Google Authentication

Pulse supports **"Continue with Google"** via Google OAuth 2.0 PKCE flow. Credentials are stored securely in OS keyring and refresh tokens are used to silently renew expired access tokens.

### Setup

1. Go to [Google Cloud Console -> APIs & Credentials](https://console.cloud.google.com/apis/credentials).
2. Create an OAuth 2.0 Client ID - **Application type: Web application**.
3. Add `http://localhost:8080` to **Authorized redirect URIs**.
4. Copy the Client ID and Secret to your `.env`:

```env
GOOGLE_CLIENT_ID=your_client_id_here
GOOGLE_CLIENT_SECRET=your_client_secret_here
GOOGLE_REDIRECT_URI=http://localhost:8080
```

### Commands

```powershell
pulse login          # Open Google sign-in in your browser
pulse logout         # Sign out and clear stored tokens
pulse whoami         # Show your name, email, and username
```

---

## Remote Sandbox Execution (`pulse-remote`)

Pulse supports remote code execution via `RemoteSandboxBackend` when local Docker/Podman is unavailable. This ensures untrusted AI code is never executed unsafely on the local host without explicit user opt-in (`unsafe_host_execution=True`).

### Architecture & Features
- **Strict Isolation**: The remote worker wraps `DockerBackend` on a dedicated remote host, preserving container capabilities (`--cap-drop=ALL`), read-only root filesystems, and memory/CPU limits.
- **Authenticated Transport**: Secures client-server communication using Bearer token authentication and TLS (`wss://`).
- **Multi-Tenant Isolation**: Scopes executions by tenant ID derived from authentication tokens.
- **Copy-on-Write Workspace Sync**: Encapsulates workspace edits in compressed `.tar.gz` overlays with strict symlink and path traversal (ZipSlip) protections.

### Server Setup

Start the remote worker service on an isolated host with Docker running:

```bash
export PULSE_REMOTE_HOST="0.0.0.0"
export PULSE_REMOTE_PORT="8080"
export PULSE_REMOTE_TOKEN="your-secure-token"

# Run the remote server daemon
pulse-remote
```

### Client Configuration

Configure Pulse on local machines to delegate isolated execution to the remote server:

```bash
export PULSE_REMOTE_URL="wss://remote-server:8080"
export PULSE_REMOTE_TOKEN="your-secure-token"
```

---

## Agent Orchestrator & Safety Management

`AgentOrchestrator` intercepts every prompt, queries `RepositoryIndex`, and deterministically routes read-only or symbol-search intents directly to tools. Complex intents are forwarded to the LLM. `SafetyManager` categorises every action as `LOW` (read), `MEDIUM` (edit/test), or `HIGH` (delete/shell), blocking `HIGH` risk actions without explicit user confirmation and writing every decision to the audit log.

---

## Autonomous Execution Loop

`AutonomousLoop` runs up to a configurable step limit (default **5 turns**). Each turn:

1. Evaluate prompt and execute via `AgentOrchestrator`
2. Assess action risk via `SafetyManager`
3. Run test verification via `VerificationEngine`
4. Record file mutations via `MutationTracker`
5. Write a JSON checkpoint to `.agent/checkpoints/`

The loop terminates early on success, safety rejection, or reaching the step limit.

---

## Hierarchical DAG Planner

`DAGPlanner` decomposes multi-file features into a **Directed Acyclic Graph** of `DAGTaskNode` execution steps. Each node declares:
- `target_files` - files it creates or modifies
- `inputs` / `outputs` - data dependencies
- `dependencies` - which tasks must complete first

`get_execution_order()` performs a topological sort and raises `ValueError` on cycle detection.

---

## AST Impact Analyzer

`ASTImpactAnalyzer` parses every Python file in the workspace to build a symbol cross-reference map. `get_affected_files(symbol_name)` returns all files that reference the given symbol - enabling safe, scoped refactoring across the entire repository without re-indexing external tools.

---

## Episodic Memory & Rule Synthesizer

`EpisodicMemory` records every execution trace (prompt, error, resolution) in a workspace SQLite database (`.agent/episodic-memory.sqlite3`). `search_similar_resolutions(query)` performs fuzzy substring matching to retrieve relevant past fixes before a new repair attempt.

`RuleSynthesizer` scans all traces, counts recurring error patterns, and generates Markdown guideline files under `.agent/rules/` for each pattern that exceeds the configured frequency threshold (default: **2 occurrences**). Generated rules are git-ignored by default and updated on each synthesizer run.

---

## Telemetry & Cost Tracking

`CostTracker` records prompt and completion token counts across OpenAI, Gemini, and OpenRouter models, applies per-model pricing, and raises `BudgetExceededError` if session token or dollar limits are exceeded. `TelemetryLogger` appends structured JSONL metric events (step, tool, duration, success) to `.agent/logs/telemetry.jsonl`.

Configure limits in `.env`:
```
PULSE_MAX_SESSION_TOKENS=100000
PULSE_MAX_SESSION_COST=1.00
```

---

## Provider Failover

`FailoverProvider` wraps a primary and secondary `LLMProvider`. If the primary raises any exception (timeout, HTTP error, rate limit), the request is transparently retried against the secondary. Configure via `agent.config.json`:
```json
"model": {
  "provider": "openrouter",
  "fallbackProvider": "gemini"
}
```

---

## VS Code Extension

**Pulse Studio for VS Code** is a modern, feature-rich local client for the Pulse JSON-RPC server. The extension (`vscode-extension/`) supports both **stdio JSON-RPC** (`PulseRpcClient`, no server required) and **WebSocket** (`ws://127.0.0.1:8765` via `pulse serve`).

### Local Development Setup

1. From the Pulse workspace, run `pulse serve`.
2. In `vscode-extension/`, run `npm install` then `npm run compile`.
3. Open `vscode-extension/` in VS Code and press `F5` to launch an Extension Development Host.

Change `pulse.serverUrl` only when you intentionally run Pulse on a different local endpoint.

### Features
- **Modern Pulse Chat sidebar** — a beautifully crafted Dark Mode UI with glassmorphism; prompt Pulse about the workspace
- **Simulated Streaming** — real-time event playback showing reasoning, planning, and task progress
- **Agent Status** — visual indicator of what Pulse is currently doing (idle, thinking, working)
- **Inline completions** — `PulseInlineEditProvider` sends document context and renders diff suggestions
- **Diagnostics Code Actions** — "Fix with Pulse" appears on any compiler/linter error; clicking sends the diagnostic to `pulse.explainDiagnostics` and shows the explanation inline
- **Terminal error debugger** — when a terminal exits with a non-zero code, Pulse prompts to debug; `pulse.debugTerminalError` sends terminal context to the backend for analysis
- **Command Palette prompts** and workspace verification
- **Explain and review actions** for the editor selection and Quick Fix menu

### RPC Methods
- **Stdio path**: `plan()`, `executeTool()`, `rollback()`, `getStatus()`, `explainDiagnostics()`, `runCommand()`, `applyPatch()`
- **WebSocket path**: `pulse.health`, `pulse.askStream`, `pulse.codeAction`, `pulse.command`

> **Note:** The RPC server deliberately rejects edit and rollback requests because their existing Pulse approval workflow requires an interactive terminal.

---

## Repository intelligence

`pulse index` stores a local index at `.agent/repository-index.json`. `pulse search` refreshes the index and ranks filename, symbol, and identifier-term matches. The project agent uses the highest-ranked results as approved context before any LLM call.

---

## Long-term memory

Durable context, preferences, and task summaries are stored in `.agent/pulse-memory.sqlite3`. Relevant memories are injected as approved context before planned work. Use `pulse memory --set key value` to save a preference and `pulse memory --query text` to inspect relevant entries.

---

## Multi-agent workflow

Non-tool requests run through an async role pipeline: **Planner -> Coding -> Reviewer -> Testing -> response**. Each role is an adapter over the core `Agent`, keeping planning, context, and provider behavior consistent. `AgentManager` is injectable for specialized roles or autonomous workflows.

---

## Git intelligence

`pulse git` reports branch, HEAD, change count, additions/deletions, and a conventional commit suggestion from the diff. Git state is captured before and after every approved edit and stored in `mutations.jsonl`.
