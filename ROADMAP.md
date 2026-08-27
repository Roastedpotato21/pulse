# Pulse Industry-Grade Agent Roadmap

## North Star

Pulse must be a dependable coding agent, not a collection of demos. Every
change must preserve clear ownership boundaries, fail safely, be observable,
be tested at the appropriate layer, and have an explicit rollback or recovery
story. Features do not ship merely because they work in a happy-path demo.

## Engineering Principles

- **Correctness before autonomy:** agents may act only inside durable,
  permissioned boundaries.
- **Explicit state machines:** execution, approval, mutation, recovery, and
  remote work use persisted states and fenced transitions.
- **Fail closed:** unavailable isolation, ambiguous execution outcome, missing
  authorization, or unsupported policy rejects the operation.
- **Small interfaces, strong contracts:** orchestration depends on protocols
  and typed domain models, not concrete providers or transport details.
- **Evidence over claims:** every reliability or security assertion has an
  adversarial test, integration test, or production signal.
- **Human control:** destructive, external, and high-impact actions remain
  reviewable and reversible where technically possible.

## Phase 0 — Baseline and Architecture Contract

Establish the system boundaries before expanding capability.

Must have:

- A documented architecture separating CLI/RPC, orchestration, planning,
  execution, sandboxing, state, providers, and observability.
- Versioned domain contracts for tasks, tool calls, approvals, mutations,
  sandbox execution, and remote execution.
- One authoritative task/execution state machine with allowed transitions and
  invariants documented beside the code.
- Dependency-direction rules and import-boundary tests to prevent coupling
  orchestration directly to provider or transport internals.
- Formatting, linting, type checking, test, dependency audit, and secret scan
  as required CI gates.

Exit criteria:

- Architecture decision records exist for durable state, execution isolation,
  provider abstraction, and approval policy.
- No known unreachable production paths, duplicate lifecycle implementations,
  or unowned persistence schema migrations.
- Clean static-analysis and full-test baseline on every supported platform.

## Phase 1 — Durable Execution Core

Make agent work resumable and safe under crashes, retries, and contention.

Must have:

- Lease fencing, optimistic concurrency, durable checkpoints, and idempotency
  keys for all task mutations.
- Explicit recovery states for local, remote, cancelled, timed-out, and
  unknown outcomes.
- Automatic binding of a task to its remote execution ID at dispatch time;
  recovery must never depend on caller-supplied metadata.
- Bounded retries with backoff, retry classification, retry budgets, and
  dead-letter/manual-resolution states.
- Transactional outbox/audit records for lifecycle events so state and
  telemetry cannot diverge silently.
- Crash, stale-worker, split-brain, process-reuse, and reconnect tests.

Exit criteria:

- No task is duplicated after process loss, lease expiry, reconnect, or
  manager restart.
- Ambiguous external outcomes remain quarantined until reconciled or manually
  resolved.
- Recovery is exercised in integration tests against SQLite and a real remote
  worker.

## Phase 2 — Safe Tool and Sandbox Platform

Treat every model-produced action as untrusted input.

Must have:

- Typed tool schemas, parameter validation, allowlisted capabilities, and
  structured results/errors.
- Central policy engine with user identity, workspace scope, risk level,
  approval requirements, and audit reason.
- Strong container isolation by default; unsafe host execution remains an
  explicit development-only mode with visible warnings.
- Network egress, secret exposure, filesystem writes, process lifetime,
  resources, and artifact extraction enforced by the backend—not prompts.
- Remote worker authentication, mTLS, tenant isolation, credential rotation,
  durable execution store, retention, health checks, and deployment guidance.
- Security fuzzing for paths, archives, shell arguments, output handling,
  policy bypasses, and cancellation/process trees.

Exit criteria:

- Security policy tests run against actual Docker/remote environments, not
  only mocks.
- Unsupported enforcement modes fail closed.
- A threat model and incident-response/credential-revocation procedure are
  reviewed and versioned.

## Phase 3 — Agent Reasoning and Planning Quality

Build predictable, inspectable behavior rather than opaque autonomous loops.

Must have:

- A structured agent loop: observe, plan, select tool, act, verify, reflect,
  checkpoint, and terminate.
- Typed plans with dependencies, acceptance criteria, budgets, and clear
  stop conditions.
- Context assembly with provenance, token budgets, relevance ranking, and
  redaction; no uncontrolled prompt accumulation.
- Tool-result grounding: conclusions that affect code or users cite observed
  files, commands, tests, or external sources.
- Model/provider routing by task class, capability, cost, latency, and safety;
  deterministic fallbacks and consistent error semantics.
- Explicit uncertainty handling and escalation when missing context or user
  authority changes the action materially.

Exit criteria:

- Agent traces explain why each consequential tool/action was selected.
- Planning and execution remain correct across model/provider substitution.
- Budget limits, cancellation, and user approval interrupt work cleanly.

## Phase 4 — Code Change Quality System

Make code modifications reviewable, minimal, and verifiably correct.

Must have:

- Repository maps, symbol-aware search, change-impact analysis, and scoped
  edit plans before modifications.
- Patch generation with preconditions, diffs, formatting, type checks, and
  targeted tests selected from impact analysis.
- Mutation ledger with before/after hashes, rollback data, approval links, and
  semantic commit messages.
- Independent verification pass that can reject the executor's result.
- Test-generation and repair bounded by policy, budget, and a maximum attempt
  count; never mask a failure by weakening tests.
- First-class support for worktrees/branches so multi-step work does not
  corrupt a user's active workspace.

Exit criteria:

- Every autonomous code edit has an auditable plan, diff, verification record,
  and rollback path.
- Regression tests prove the verifier can catch deliberately injected bad
  patches.
- Human reviewers can reproduce agent conclusions from stored evidence.

## Phase 5 — Evaluation, Reliability, and Observability

Measure the agent continuously in the conditions where it will fail.

Must have:

- A versioned evaluation suite: repository navigation, bug fixes, feature
  work, tool safety, recovery, prompt injection, and multi-step workflows.
- Golden traces and deterministic fakes for unit tests; isolated real-provider
  and real-sandbox integration environments for end-to-end tests.
- Metrics for task success, verification pass rate, rollback rate, tool error
  rate, recovery duration, cost, latency, and unsafe-policy rejections.
- OpenTelemetry-compatible structured tracing with correlation IDs from user
  request through task, tool, remote execution, and mutation.
- Error budgets, alert thresholds, dashboards, and runbooks for remote worker,
  provider, persistence, and security incidents.
- Load, soak, fault-injection, and upgrade/migration testing.

Exit criteria:

- Release candidates demonstrate target reliability and safety thresholds on
  the evaluation suite.
- Every production incident class has a dashboard, alert, owner, and runbook.
- Performance/cost regressions block release automatically.

## Phase 6 — Product, Multi-User, and Release Engineering

Prepare Pulse for responsible distribution and long-term maintenance.

Must have:

- Clear local, team, and hosted deployment modes with authentication,
  authorization, tenancy, data retention, and privacy policies.
- Stable CLI/RPC API versioning, compatibility policy, migration tooling, and
  user-visible deprecation notices.
- Production OAuth configuration, secure credential storage policy, token
  revocation, and account/session lifecycle tests.
- Signed, reproducible packages; SBOM, dependency provenance, license,
  changelog, release notes, and vulnerability response process.
- Cross-platform installer/upgrade/uninstall testing and a clean first-run
  experience.
- Documentation that distinguishes implemented guarantees from experimental
  features and unsupported configurations.

Exit criteria:

- A clean-machine release test succeeds on all supported platforms.
- CI produces signed artifacts and publishes only after security, quality,
  evaluation, and compatibility gates pass.
- Public documentation, support policy, and release checklist are complete.

## Definition of Done for Every Change

1. The responsibility belongs to the correct architectural layer.
2. Interfaces, state transitions, and failure behavior are explicit and typed.
3. The change has focused unit tests and the relevant integration/adversarial
   test coverage.
4. Lint, types, tests, security checks, and migration checks pass.
5. Telemetry/audit coverage exists for consequential behavior.
6. The user impact, rollout risk, and rollback/recovery path are documented.
7. No test is weakened to make the implementation appear successful.
