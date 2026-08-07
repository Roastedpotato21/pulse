"""Industry-Grade Secure Sandbox Subsystem for Pulse.

Provides workspace isolation, rootless container execution, fine-grained
policy permissions, secret protection, process management, and structured
audit logging.
"""

from pulse.sandbox.api import Sandbox, SandboxSession
from pulse.sandbox.audit import StructuredAuditEntry, StructuredAuditLogger
from pulse.sandbox.backend import ContainerBackend, DockerBackend, HostBackend
from pulse.sandbox.errors import (
    SandboxResourceError,
    SandboxSecurityError,
    SandboxUnavailableError,
)
from pulse.sandbox.filesystem import CoWFilesystem, CoWTransaction
from pulse.sandbox.git_safe import SafeGit
from pulse.sandbox.path_validator import PathValidationError, PathValidator
from pulse.sandbox.policy import ActionType, PolicyDecision, PolicyRule, SandboxPolicy
from pulse.sandbox.process import ProcessManager, ProcessResult
from pulse.sandbox.project import ProjectSandbox
from pulse.sandbox.python_safe import SafePython
from pulse.sandbox.resources import ResourceLimiter, ResourceLimits
from pulse.sandbox.secrets import SecretScrubber

__all__ = [
    "ActionType",
    "CoWFilesystem",
    "CoWTransaction",
    "ContainerBackend",
    "DockerBackend",
    "HostBackend",
    "PathValidationError",
    "PathValidator",
    "PolicyDecision",
    "PolicyRule",
    "ProcessManager",
    "ProcessResult",
    "ProjectSandbox",
    "ResourceLimiter",
    "ResourceLimits",
    "SafeGit",
    "SafePython",
    "Sandbox",
    "SandboxPolicy",
    "SandboxResourceError",
    "SandboxSecurityError",
    "SandboxSession",
    # New security error types
    "SandboxUnavailableError",
    "SecretScrubber",
    "StructuredAuditEntry",
    "StructuredAuditLogger",
]
