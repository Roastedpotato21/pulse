"""Sandbox Execution Lifecycle Management.

Defines the authoritative state machine for a sandbox execution.
Ensures valid state transitions and crash-safe tracking.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any


class LifecycleState(str, Enum):
    """The authoritative execution state."""
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    COMPLETING = "COMPLETING"
    FAILED = "FAILED"
    CLEANING = "CLEANING"
    FINALIZED = "FINALIZED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


# Valid transition graph
VALID_TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
    LifecycleState.CREATED: {LifecycleState.STARTING, LifecycleState.FAILED, LifecycleState.CLEANING},
    LifecycleState.STARTING: {LifecycleState.RUNNING, LifecycleState.FAILED, LifecycleState.CLEANING},
    LifecycleState.RUNNING: {LifecycleState.COMPLETING, LifecycleState.STOPPING, LifecycleState.FAILED},
    LifecycleState.STOPPING: {LifecycleState.FAILED, LifecycleState.CLEANING},
    LifecycleState.COMPLETING: {LifecycleState.CLEANING, LifecycleState.FAILED},
    LifecycleState.FAILED: {LifecycleState.CLEANING},
    LifecycleState.CLEANING: {LifecycleState.FINALIZED, LifecycleState.RECOVERY_REQUIRED},
    LifecycleState.FINALIZED: set(),  # Terminal
    LifecycleState.RECOVERY_REQUIRED: set(),  # Terminal (requires external admin intervention)
}


class InvalidStateTransitionError(Exception):
    """Raised when an execution attempts an invalid lifecycle transition."""


class SandboxExecution:
    """Tracks a single sandbox execution lifecycle."""

    def __init__(self, execution_id: str, audit_logger: Any = None) -> None:
        self.execution_id = execution_id
        self._state = LifecycleState.CREATED
        self.created_at = time.time()
        self.history: list[tuple[float, LifecycleState]] = [(self.created_at, self._state)]
        self.audit_logger = audit_logger

    @property
    def state(self) -> LifecycleState:
        return self._state

    def transition(self, new_state: LifecycleState) -> None:
        """Transition the execution to a new state."""
        allowed = VALID_TRANSITIONS.get(self._state, set())
        if new_state not in allowed:
            # Self-healing: if we try to go to FAILED from a terminal state, just ignore or log
            if new_state == LifecycleState.FAILED and self._state in {LifecycleState.FINALIZED, LifecycleState.RECOVERY_REQUIRED}:
                return
            # Allow skipping straight to FAILED or CLEANING from early states for fast-fail
            raise InvalidStateTransitionError(
                f"Cannot transition execution {self.execution_id} from {self._state.value} to {new_state.value}."
            )
        
        self._state = new_state
        self.history.append((time.time(), new_state))
        
        if self.audit_logger:
            self.audit_logger.record(
                action="lifecycle-transition",
                target=self.execution_id,
                decision="allow",
                detail=f"Execution state transitioned to {new_state.value}",
            )

    def serialize(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "state": self._state.value,
            "created_at": self.created_at,
            "history": [(ts, state.value) for ts, state in self.history],
        }
