# Pulse Architecture Contract

## Purpose

Pulse is a permissioned coding agent. Its architecture is designed so that a
model response cannot bypass policy, durable state, verification, or execution
isolation by importing a convenient lower-level implementation.

## Layer Direction

Dependencies point inward. A layer may depend only on the layer beneath it or
on a protocol/model owned by a lower layer.

```text
CLI / RPC
  -> Orchestration and agent workflow
    -> Planning, tools, verification, task coordination
      -> Domain contracts and durable state
        -> Providers | Sandbox backends | storage adapters | telemetry adapters
```

### Layer responsibilities

| Layer | Owns | Must not own |
| --- | --- | --- |
| CLI and RPC | Input/output, presentation, API compatibility | Policy decisions, direct provider calls, database writes |
| Orchestration and agent workflow | Prompt lifecycle, plan execution, cancellation propagation | Shell execution, persistence implementation, credential handling |
| Planning, tools, verification | Typed intent, scoped plans, approval requests, evidence collection | Provider transport and sandbox implementation details |
| Task coordination | Durable task lifecycle, leases, checkpoints, fencing, recovery | UI behavior and provider-specific reasoning |
| Sandbox | Policy enforcement, isolated execution, artifacts, execution lifecycle | Agent planning and provider selection |
| Providers | Model request/response translation and normalized errors | Tool authorization, filesystem access, task lifecycle mutation |
| Storage and telemetry adapters | Persistence and observation mechanics | Business-policy decisions |

## Domain Contracts

The following contracts are the stable integration points. New behavior must
extend one of them rather than coupling across layers.

- **Task:** durable user-visible unit of work, identified by `Task.id`.
- **Lease fence:** `(owner_id, lease_epoch)` capability required to mutate a
  running task.
- **Checkpoint:** durable resumable state belonging to a task and step.
- **Task event:** append-only lifecycle observation for UI, audit, and metrics.
- **Sandbox execution:** isolated command attempt identified by an execution
  ID, with policy, result, artifact, and terminal reason.
- **Remote execution:** remote sandbox attempt with an execution ID persisted
  against the task before work starts.
- **Tool call:** typed, policy-authorized operation with validated input and a
  structured result/error.
- **Approval:** explicit user authorization linked to the requested action and
  its scope.
- **Mutation:** a reversible workspace change with before/after evidence.

## Task Lifecycle Contract

`TaskStatus` is persisted in SQLite and is authoritative after restart.

```text
PENDING -> QUEUED -> RUNNING -> COMPLETED
                 |       |  -> FAILED -> QUEUED (bounded retry)
                 |       |  -> PAUSED -> QUEUED
                 |       |  -> CANCELLED
                 |       `-> RECOVERY_PENDING
                 `-> CANCELLED

RECOVERY_PENDING -> QUEUED | COMPLETED | FAILED
```

### Lifecycle invariants

1. Only a worker holding the current unexpired lease fence may mutate a
   `RUNNING` task.
2. Recovery first persists `RECOVERY_PENDING`; it never retries an ambiguous
   remote outcome automatically.
3. A live recorded local owner PID blocks requeue unless that executor
   acknowledges termination. Absence from another process's in-memory registry
   is not evidence that the executor stopped.
4. `COMPLETED`, `FAILED`, and `CANCELLED` clear active ownership. A completed
   result is immutable except through an explicit new task/retry workflow.
5. **Phase 1 requirement:** remote dispatch persists its remote execution ID
   before submission. Recovery reconciles that exact ID, not a generated
   replacement. This binding is not yet automatic in the current dispatch
   path and must not be claimed as a release guarantee.

## Sandbox Lifecycle Contract

Sandbox execution uses the `SandboxExecution` state machine:

```text
CREATED -> STARTING -> RUNNING -> COMPLETING -> CLEANING -> FINALIZED
                       |             |
                       v             v
                    STOPPING       FAILED -> CLEANING
                                      |
                                      v
                              RECOVERY_REQUIRED
```

`RECOVERY_REQUIRED` is terminal and requires explicit operator action. Lack of
an available secure backend, an unenforceable policy, or an unknown external
outcome must fail closed.

## Dependency Rules Enforced in Tests

- `pulse.core` is domain-only and must not import orchestration, providers,
  sandbox, CLI/RPC, or persistence implementations.
- `pulse.providers` must not import agent, orchestration, task-manager, or
  sandbox modules.
- `pulse.sandbox` must not import agent, orchestration, planner, or provider
  modules.
- `pulse.task_manager` must not import CLI/RPC, agent orchestration, or a
  provider implementation.

Exceptions require an architecture decision record and an accompanying
boundary-test update explaining why the dependency is safe.

## Change Checklist

Before merging a behavior change, identify its owning layer, contract,
lifecycle transition, failure mode, audit signal, and recovery/rollback path.
