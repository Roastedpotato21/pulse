"""Secure sandbox subsystem public exports.

The package keeps these exports lazy so importing a narrow helper such as
``pulse.sandbox.secrets`` does not initialize the full sandbox stack.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ActionType": "pulse.sandbox.policy",
    "CoWFilesystem": "pulse.sandbox.filesystem",
    "CoWTransaction": "pulse.sandbox.filesystem",
    "ContainerBackend": "pulse.sandbox.backend",
    "DockerBackend": "pulse.sandbox.backend",
    "ExecutionMetrics": "pulse.sandbox.resources",
    "HostBackend": "pulse.sandbox.backend",
    "PathValidationError": "pulse.sandbox.path_validator",
    "PathValidator": "pulse.sandbox.path_validator",
    "PolicyDecision": "pulse.sandbox.policy",
    "PolicyRule": "pulse.sandbox.policy",
    "ProcessManager": "pulse.sandbox.process",
    "ProcessResult": "pulse.sandbox.process",
    "ProjectSandbox": "pulse.sandbox.project",
    "ResourceController": "pulse.sandbox.resources",
    "ResourceLimitExceeded": "pulse.sandbox.resources",
    "ResourceLimiter": "pulse.sandbox.resources",
    "ResourceLimits": "pulse.sandbox.resources",
    "ResourceMonitor": "pulse.sandbox.resources",
    "ResourcePolicy": "pulse.sandbox.resources",
    "SafeGit": "pulse.sandbox.git_safe",
    "SafePython": "pulse.sandbox.python_safe",
    "Sandbox": "pulse.sandbox.api",
    "SandboxConcurrentModificationError": "pulse.sandbox.errors",
    "SandboxPolicy": "pulse.sandbox.policy",
    "SandboxResourceError": "pulse.sandbox.errors",
    "SandboxSecurityError": "pulse.sandbox.errors",
    "SandboxSession": "pulse.sandbox.api",
    "SandboxUnavailableError": "pulse.sandbox.errors",
    "SecretScrubber": "pulse.sandbox.secrets",
    "StructuredAuditEntry": "pulse.sandbox.audit",
    "StructuredAuditLogger": "pulse.sandbox.audit",
    "TimeoutExceeded": "pulse.sandbox.resources",
}

__all__ = tuple(sorted(_EXPORTS))


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = import_module(_EXPORTS[name])
    value = getattr(module, name)
    globals()[name] = value
    return value
