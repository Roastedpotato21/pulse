# Pulse

**Pulse** is an autonomous, multi-capability project agent CLI written in Python and managed with `uv`. It combines repository intelligence, hierarchical planning, safety-gated execution, episodic memory, cost tracking, and VS Code IDE integration.

## Setup

1. Copy `.env.example` to `.env`.
2. Add your API keys (`OPENROUTER_API_KEY`, `GEMINI_API_KEY`, or `OPENAI_API_KEY`).
3. Install and sync:

```powershell
uv sync
```

4. Install the local CLI:

```powershell
uv tool install --editable .
```

5. Check the setup:

```powershell
pulse doctor
```

Ask your first question:

```powershell
pulse ask "What files are in this project?"
```

---

## Python module layout

| Module | Purpose |
|---|---|
| `pulse.cli` | Command line entry point |
| `pulse.agent` | Single-agent orchestration and context selection |
| `pulse.orchestration` | `AgentOrchestrator` — intent routing via Repository Intelligence |
| `pulse.safety` | `SafetyManager`, `RiskLevel` — LOW / MEDIUM / HIGH action risk assessment |
| `pulse.planner.execution_loop` | `AutonomousLoop` — multi-turn tool execution with checkpointing |
| `pulse.planner.dag_planner` | `DAGPlanner` — hierarchical DAG decomposition of multi-file features |
| `pulse.providers.failover` | `FailoverProvider` — transparent secondary provider fallback |
| `pulse.telemetry` | `CostTracker`, `TelemetryLogger` — token/cost budgets and structured metrics |
| `pulse.refactor.impact_analyzer` | `ASTImpactAnalyzer` — AST cross-reference symbol impact analysis |
| `pulse.episodic` | `EpisodicMemory` — SQLite execution trace storage |
| `pulse.rule_synthesizer` | `RuleSynthesizer` — auto-generated `.agent/rules` from repeated error patterns |
| `pulse.provider` | OpenRouter / Gemini / OpenAI streaming providers |
| `pulse.repository` | Async incremental repository index and semantic search |
| `pulse.verification` | Async test-runner detection, diagnostics, and repair/retry |
| `pulse.git` | Git status, diff analysis, and commit suggestions |
| `pulse.memory` | SQLite long-term context, preferences, and task memory |
| `pulse.multi_agent` | Async Planner → Coder → Reviewer → Tester role pipeline |
| `pulse.mutations` | File mutation tracking, snapshots, diffs, and rollback |
| `pulse.rpc` | JSON-RPC 2.0 WebSocket adapter for IDE clients |
| `pulse.audit` | Session action log |
| `pulse.sandbox` | Workspace permission boundary |
| `pulse.config` | Project config and `.env` loading |
| `pulse.tool_registry` | Async Tool interface, permission gates, and concurrent dispatch |
| `pulse.tools` | Built-in tools: status, doctor, edit, rollback, mutations, verify, git |

---

## Commands

```
pulse                          Interactive project chat
pulse ask <question>           Single-shot project question
pulse status                   Show agent configuration
pulse doctor                   Check env, config, and provider readiness
pulse edit <file> <content>    Propose a diff, approve or discard
pulse rollback                 Restore latest approved edit snapshot
pulse mutations [--last]       Inspect tracked file mutations
pulse index                    Build / refresh repository index
pulse search <query>           Lexical-semantic file search
pulse symbols <file>           List imports, classes, functions for a file
pulse verify                   Run project test suite
pulse git                      Branch status, diff, commit suggestion
pulse memory [--query <text>]  Inspect or set long-term memory
pulse serve                    Start loopback JSON-RPC WebSocket server
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
- `target_files` — files it creates or modifies
- `inputs` / `outputs` — data dependencies
- `dependencies` — which tasks must complete first

`get_execution_order()` performs a topological sort and raises `ValueError` on cycle detection.

---

## AST Impact Analyzer

`ASTImpactAnalyzer` parses every Python file in the workspace to build a symbol cross-reference map. `get_affected_files(symbol_name)` returns all files that reference the given symbol — enabling safe, scoped refactoring across the entire repository without re-indexing external tools.

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

The extension (`vscode-extension/`) connects to the Pulse backend over **stdio JSON-RPC** (`PulseRpcClient`) rather than WebSocket — no server process required.

### Features
- **Chat sidebar** — prompt Pulse about the workspace
- **Inline completions** — `PulseInlineEditProvider` sends document context and renders diff suggestions
- **Diagnostics Code Actions** — "Fix with Pulse" appears on any compiler/linter error; clicking sends the diagnostic to `pulse.explainDiagnostics` and shows the explanation inline
- **Terminal error debugger** — when a terminal exits with a non-zero code, Pulse prompts to debug; `pulse.debugTerminalError` sends terminal context to the backend for analysis
- **RPC methods**: `plan()`, `executeTool()`, `rollback()`, `getStatus()`, `explainDiagnostics()`, `runCommand()`, `applyPatch()`

Run the extension from `vscode-extension/` after `pulse serve` is running (WebSocket path) or launch it directly for the stdio-RPC path.

---

## Repository intelligence

`pulse index` stores a local index at `.agent/repository-index.json`. `pulse search` refreshes the index and ranks filename, symbol, and identifier-term matches. The project agent uses the highest-ranked results as approved context before any LLM call.

---

## Long-term memory

Durable context, preferences, and task summaries are stored in `.agent/pulse-memory.sqlite3`. Relevant memories are injected as approved context before planned work. Use `pulse memory --set key value` to save a preference and `pulse memory --query text` to inspect relevant entries.

---

## Multi-agent workflow

Non-tool requests run through an async role pipeline: **Planner → Coding → Reviewer → Testing → response**. Each role is an adapter over the core `Agent`, keeping planning, context, and provider behavior consistent. `AgentManager` is injectable for specialized roles or autonomous workflows.

---

## Git intelligence

`pulse git` reports branch, HEAD, change count, additions/deletions, and a conventional commit suggestion from the diff. Git state is captured before and after every approved edit and stored in `mutations.jsonl`.

---

If `pulse` or `uv` is not recognized in an already-open terminal, restart the terminal so the PATH update applies:

```
C:\Users\sindh\.local\bin
C:\Users\sindh\AppData\Roaming\Python\Python311\Scripts
```
