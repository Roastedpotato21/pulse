# GAP-02 Lease-Bound Execution Remediation

## Root cause and previous architecture

`process_queue()` awaited `worker_func(task)` while a separate heartbeat
renewed its lease. If that heartbeat lost ownership, it stopped but left the
worker running. OCC protected persisted task state, but could not prevent a
stale worker from issuing external side effects while a recovered worker began
the same task.

## Remediated architecture

Each queue execution is supervised as a worker task paired with its heartbeat
task. `asyncio.wait(..., FIRST_COMPLETED)` observes both. A heartbeat turns a
lost lease into `LeaseLostError`; the supervisor cancels and awaits the worker.
Conversely, when work completes, the supervisor reads the authoritative task
record and verifies that it is still RUNNING and owned by this worker before
allowing completion.

## Lease-loss and race handling

Lease loss cancels the local async worker, prevents completion and failure
handling by that worker, and leaves recovery to the new owner. If worker
completion and heartbeat loss are observed together, lease loss wins. The
authoritative ownership check before completion is a second guard against a
transition that occurs just after the worker returns. OCC and stale-worker
mutation checks remain in place as defense in depth.

## Cleanup guarantees

The supervisor explicitly cancels and awaits both child tasks for successful
work, worker failure, lost lease, and cancellation of `process_queue()`. A
failed worker follows the existing retry/failure lifecycle. Heartbeats that
fail to renew are fatal to the execution rather than silently exiting.

## Recovery and remote tasks

Existing local recovery remains unchanged: an expired local RUNNING task is
returned to QUEUED. Remote tasks remain RECOVERY_PENDING; this change does not
restart or reconcile remote work, which remains GAP-07 scope.

## Adversarial coverage

`tests/test_task_manager_lease_execution.py` covers lease-loss cancellation,
recovery by a second worker, healthy heartbeats, worker failures,
`process_queue()` cancellation, and completion/lease-loss races. The
concurrency and hard-crash recovery suites retain coverage of OCC, stale-worker
rejection, and recovery behavior.

## Limitation

Async cancellation stops Pulse from continuing its worker code and mutations.
It cannot undo an external request, process, sandbox action, or API call that
was already accepted. Those integrations must support their own cancellation
or reconciliation semantics.
