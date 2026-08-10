"""High-level Sandbox API facade with Dependency Injection.

Unified entry point integrating SandboxPolicy, PathValidator, SecretScrubber,
StructuredAuditLogger, ResourceLimiter, ContainerBackend, and CoWFilesystem.

Security hardening:
    - Fail-secure: raises SandboxUnavailableError if no container backend available
      and unsafe_host_execution is not explicitly True.
    - read_file() uses TOCTOU-safe PathValidator.safe_read() with size limits.
    - execute_command() determines isolation_level and logs it in audit.
    - Host fallback requires explicit opt-in and logs UNSAFE_HOST warnings.
    - initialize() must be called before first execute_command().
"""

from __future__ import annotations

import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from pulse.sandbox.audit import StructuredAuditLogger
from pulse.sandbox.backend import ContainerBackend, DockerBackend, HostBackend
from pulse.sandbox.errors import SandboxUnavailableError, SandboxUnsupportedPolicyError
from pulse.sandbox.filesystem import CoWFilesystem, CoWTransaction
from pulse.sandbox.network import NetworkEnforcementLevel, NetworkMode, NetworkPolicy
from pulse.sandbox.path_validator import PathValidator
from pulse.sandbox.policy import ActionType, PolicyDecision, SandboxPolicy
from pulse.sandbox.process import ProcessResult
from pulse.sandbox.resources import ResourceLimits, ResourcePolicy
from pulse.sandbox.secrets import (
    SecretEnforcementLevel,
    SecretMode,
    SecretPolicy,
    SecretScrubber,
)

# SecurityWarning is not available in all Python builds; define a fallback.
try:
    _SecurityWarning = SecurityWarning  # type: ignore[name-defined]
except NameError:
    class _SecurityWarning(UserWarning):  # type: ignore[no-redef]
        """Fallback warning class for security-sensitive operations."""


@dataclass
class SandboxSession:
    """Represents an active sandboxed agent session."""

    session_id: str
    workspace_root: Path
    created_at: float = field(default_factory=time.time)


class Sandbox:
    """Production-ready secure execution sandbox facade.

    Security architecture:
        - Fail-secure by default: no silent host fallback.
        - TOCTOU-safe file reads via PathValidator.safe_read().
        - All file writes routed through CoW transactions.
        - Container execution with workspace mounted read-only.
        - Audit logging with isolation level tracking.

    Args:
        workspace_root: Path to the workspace directory.
        policy: Policy engine for action authorization.
        allowed_external_reads: Paths outside workspace allowed for reads.
        secrets: List of secret strings to redact from output.
        limits: Resource limits for process execution.
        backend: Explicit container backend (overrides auto-detection).
        audit_log_path: Path to the audit log file.
        unsafe_host_execution: If True, allows fallback to HostBackend when
            no container engine is available. Default False (fail-secure).

    Raises:
        SandboxUnavailableError: If no container backend is available and
            unsafe_host_execution is False (after initialize() is called).
    """

    def __init__(
        self,
        workspace_root: Path,
        policy: SandboxPolicy | None = None,
        allowed_external_reads: list[Path] | None = None,
        secrets: list[str] | None = None,
        limits: ResourceLimits | ResourcePolicy | None = None,
        network_policy: NetworkPolicy | None = None,
        secret_policy: SecretPolicy | None = None,
        backend: ContainerBackend | None = None,
        audit_log_path: Path | None = None,
        unsafe_host_execution: bool = False,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.policy = policy or SandboxPolicy()
        self.network_policy = network_policy
        self.secret_policy = secret_policy
        self.validator = PathValidator(self.workspace_root, allowed_external_reads=allowed_external_reads)
        self.scrubber = SecretScrubber(secrets=secrets)
        self.limits = limits or ResourcePolicy()
        self._unsafe_host_execution = unsafe_host_execution

        log_file = audit_log_path or (self.workspace_root / ".agent" / "logs" / "audit.jsonl")
        self.audit_logger = StructuredAuditLogger(log_file, scrubber=self.scrubber)

        # Backend initialization: explicit backend or deferred to initialize()
        self._backend_explicit = backend is not None
        self.backend = backend or HostBackend()
        self._initialized = False

        self.cow = CoWFilesystem(self.workspace_root)

    async def initialize(self) -> None:
        """Select preferred container backend. Must be called before execute_command().

        Security behavior:
            1. Try Docker/Podman. If available, use it.
            2. If unavailable AND unsafe_host_execution=True: fall back to HostBackend
               with warnings and audit logging.
            3. If unavailable AND unsafe_host_execution=False: raise SandboxUnavailableError.
        """
        if self._backend_explicit:
            # Caller provided an explicit backend — respect it
            self._initialized = True
            return

        docker_be = DockerBackend()
        if await docker_be.is_available():
            self.backend = docker_be
            self._initialized = True
            self.audit_logger.record(
                action="sandbox-init",
                target=docker_be.name,
                decision="allow",
                isolation_level="container",
                detail=f"Secure container backend '{docker_be.name}' initialized.",
            )
            return

        # No container engine available
        if self._unsafe_host_execution:
            # Explicit opt-in to unsafe host execution
            warnings.warn(
                "No container engine (Docker/Podman) found. Falling back to "
                "HostBackend — untrusted code will execute directly on the host. "
                "This is NOT safe for production use with untrusted AI-generated code.",
                _SecurityWarning,
                stacklevel=2,
            )
            self.backend = HostBackend()
            self._initialized = True
            self.audit_logger.record(
                action="sandbox-init",
                target="host",
                decision="allow",
                isolation_level="host_unsafe",
                detail="WARNING: Falling back to unsafe host execution. No container isolation.",
            )
            return

        # Fail-secure: no container, no opt-in
        self.audit_logger.record(
            action="sandbox-init",
            target="none",
            decision="deny",
            isolation_level="unavailable",
            detail="No secure backend available and unsafe_host_execution=False.",
        )
        raise SandboxUnavailableError()

    def _get_isolation_level(self) -> str:
        """Determine the current isolation level for audit logging."""
        if isinstance(self.backend, HostBackend) or getattr(self.backend, "is_unsafe", False):
            return "host_unsafe"
        return "container"

    def read_file(self, relative_path: str) -> str:
        """Read a file securely within workspace boundaries after policy authorization.

        Security guarantees:
            - Policy authorization checked before any I/O.
            - TOCTOU-safe read via PathValidator.safe_read() (atomic open + validate).
            - File size enforced (MAX_FILE_SIZE prevents OOM).
            - Secret scrubbing applied to returned content.
        """
        decision = self.policy.evaluate(ActionType.READ, relative_path)
        if decision == PolicyDecision.DENY:
            self.audit_logger.record(action="read", target=relative_path, decision="deny", detail="Policy denied read access")
            raise PermissionError(f"Policy denied read access to '{relative_path}'")

        # TOCTOU-safe read: validates path, opens with O_NOFOLLOW, checks size, reads in chunks
        raw_content = self.validator.safe_read(relative_path)
        clean_content = self.scrubber.redact(raw_content)

        self.audit_logger.record(action="read", target=relative_path, decision=decision.value, detail="Read file successfully")
        return clean_content

    async def execute_command(
        self,
        command: str | list[str],
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        """Execute a shell command inside the isolated container backend.

        Security guarantees:
            - Policy authorization checked before execution.
            - If not initialized, auto-initializes (fail-secure).
            - Isolation level tracked and audit-logged.
            - Secret scrubbing applied to stdout/stderr.
        """
        # Auto-initialize if not yet done (fail-secure)
        if not self._initialized:
            await self.initialize()

        cmd_str = command if isinstance(command, str) else " ".join(command)
        network_allowed = self.policy.is_allowed(ActionType.NETWORK)
        secrets_allowed = self.policy.is_allowed(ActionType.SECRETS)
        
        # Resolve effective network policy
        effective_network_policy = self.network_policy
        if not network_allowed:
            effective_network_policy = NetworkPolicy(mode=NetworkMode.DENY_ALL)
        elif not effective_network_policy:
            if getattr(self.backend, "is_unsafe", False):
                effective_network_policy = NetworkPolicy(mode=NetworkMode.ALLOW_ALL)
            else:
                effective_network_policy = NetworkPolicy(mode=NetworkMode.DENY_ALL)

        # Resolve effective secret policy
        effective_secret_policy = self.secret_policy
        if not secrets_allowed:
            effective_secret_policy = SecretPolicy(mode=SecretMode.DENY_ALL)
        elif not effective_secret_policy:
            if getattr(self.backend, "is_unsafe", False):
                effective_secret_policy = SecretPolicy(mode=SecretMode.ALLOW_ALL)
            else:
                effective_secret_policy = SecretPolicy(mode=SecretMode.DENY_ALL)
            
        # Enforce fail-closed capability checks
        net_capability = self.backend.get_network_enforcement_capability(effective_network_policy)
        sec_capability = self.backend.get_secret_enforcement_capability(effective_secret_policy)
        
        self.audit_logger.log_network(
            destination="*",
            port=None,
            protocol="any",
            decision="allow" if effective_network_policy.mode != NetworkMode.DENY_ALL else "deny",
            backend=getattr(self.backend, "name", "unknown"),
            enforcement_level=net_capability.value,
            detail=f"Network policy mode applied: {effective_network_policy.mode.value}"
        )

        self.audit_logger.record(
            action="secrets",
            target="environment",
            decision="allow" if effective_secret_policy.mode != SecretMode.DENY_ALL else "deny",
            isolation_level=sec_capability.value,
            detail=f"Secret policy mode applied: {effective_secret_policy.mode.value}",
        )
            
        if effective_network_policy.mode != NetworkMode.DENY_ALL and not network_allowed:
            pass
        elif net_capability != NetworkEnforcementLevel.STRONGLY_ENFORCED:
            self.audit_logger.record(
                action="shell", target=cmd_str, decision="deny",
                isolation_level=self._get_isolation_level(),
                detail=f"Backend cannot strongly enforce network policy: {effective_network_policy.mode.value}",
            )
            raise SandboxUnsupportedPolicyError(
                f"Backend '{getattr(self.backend, 'name', 'unknown')}' cannot strongly enforce "
                f"network policy mode '{effective_network_policy.mode.value}'. "
                "Execution rejected to prevent silent security downgrades."
            )

        if effective_secret_policy.mode != SecretMode.DENY_ALL and not secrets_allowed:
            pass
        elif sec_capability != SecretEnforcementLevel.STRONGLY_ENFORCED:
            self.audit_logger.record(
                action="shell", target=cmd_str, decision="deny",
                isolation_level=self._get_isolation_level(),
                detail=f"Backend cannot strongly enforce secret policy: {effective_secret_policy.mode.value}",
            )
            raise SandboxUnsupportedPolicyError(
                f"Backend '{getattr(self.backend, 'name', 'unknown')}' cannot strongly enforce "
                f"secret policy mode '{effective_secret_policy.mode.value}'. "
                "Execution rejected to prevent silent credential leakage."
            )

        isolation_level = self._get_isolation_level()

        decision = self.policy.evaluate(ActionType.SHELL, cmd_str)
        if decision == PolicyDecision.DENY:
            self.audit_logger.record(
                action="shell", target=cmd_str, decision="deny",
                isolation_level=isolation_level,
                detail="Policy denied shell command execution",
            )
            return ProcessResult(
                command=cmd_str,
                exit_code=-1,
                stdout="",
                stderr="Policy denied shell execution.",
                duration_ms=0.0,
            )

        start_time = time.monotonic()
        target_dir = self.validator.validate_path(cwd or self.workspace_root)

        result = await self.backend.execute(
            command=command,
            workspace_root=self.workspace_root,
            cwd=target_dir,
            env=env,
            limits=self.limits,
            network_policy=effective_network_policy,
            secret_policy=effective_secret_policy,
        )

        clean_stdout = self.scrubber.redact(result.stdout)
        clean_stderr = self.scrubber.redact(result.stderr)
        duration_ms = (time.monotonic() - start_time) * 1000.0

        self.audit_logger.record(
            action="shell",
            target=cmd_str,
            decision=decision.value,
            exit_code=result.exit_code,
            duration_ms=duration_ms,
            container_id=getattr(self.backend, "name", "unknown"),
            isolation_level=isolation_level,
            detail=f"Executed command via {self.backend.name} backend",
        )

        # Extract overlay changes to the active workspace via CoW
        if result.overlay_path and result.overlay_path.exists():
            import shutil

            from pulse.sandbox.errors import SandboxConcurrentModificationError
            try:
                tx = None
                for item in result.overlay_path.rglob("*"):
                    if item.is_file():
                        try:
                            rel_path = item.relative_to(result.overlay_path)
                            if tx is None:
                                tx = self.create_transaction()
                            content = item.read_bytes()
                            # Try to decode as string, fallback to bytes if we ever support it.
                            # Currently stage_write expects str.
                            self.stage_write(tx, str(rel_path), content.decode("utf-8", errors="replace"))
                        except ValueError:
                            pass
                if tx and tx.staged_changes:
                    try:
                        self.commit_transaction(tx)
                    except SandboxConcurrentModificationError as e:
                        # Overlay commit conflicted with external changes
                        self.logger.record(action="commit_overlay", target=str(e.path), decision="deny", reason="concurrent_modification")
                        self.discard_transaction(tx)
            finally:
                shutil.rmtree(result.overlay_path, ignore_errors=True)

        return ProcessResult(
            command=cmd_str,
            exit_code=result.exit_code,
            stdout=clean_stdout,
            stderr=clean_stderr,
            duration_ms=duration_ms,
            timed_out=result.timed_out,
            truncated=result.truncated,
            pid=result.pid,
            overlay_path=None,  # Consumed and cleaned up
            metrics=result.metrics,
            termination_reason=result.termination_reason,
        )

    # -----------------------------------------------------------------------
    # CoW Transaction API
    # -----------------------------------------------------------------------

    def create_transaction(self) -> CoWTransaction:
        return self.cow.create_transaction()

    def stage_write(self, tx: CoWTransaction, relative_path: str, content: str) -> Path:
        decision = self.policy.evaluate(ActionType.WRITE, relative_path)
        if decision == PolicyDecision.DENY:
            self.audit_logger.record(action="write", target=relative_path, decision="deny", detail="Policy denied write access")
            raise PermissionError(f"Policy denied write access to '{relative_path}'")

        if self.scrubber.contains_explicit_secret(content):
            from pulse.sandbox.errors import SandboxSecurityError
            self.audit_logger.record(
                action="write", target=relative_path, decision="deny", 
                detail="Commit rejected: explicitly authorized secret found in staged file."
            )
            raise SandboxSecurityError(
                "Commit rejected: explicitly authorized secret found in staged file.",
                operation="stage_write", path=relative_path
            )

        path = self.cow.stage_write(tx, relative_path, content)
        self.audit_logger.record(action="write-staged", target=relative_path, decision=decision.value)
        return path

    def stage_delete(self, tx: CoWTransaction, relative_path: str) -> None:
        decision = self.policy.evaluate(ActionType.DELETE, relative_path)
        if decision == PolicyDecision.DENY:
            self.audit_logger.record(action="delete", target=relative_path, decision="deny", detail="Policy denied delete access")
            raise PermissionError(f"Policy denied delete access to '{relative_path}'")

        self.cow.stage_delete(tx, relative_path)
        self.audit_logger.record(action="delete-staged", target=relative_path, decision=decision.value)

    def preview_changes(self, tx: CoWTransaction) -> str:
        return self.cow.preview_changes(tx)

    def commit_transaction(self, tx: CoWTransaction) -> list[str]:
        modified = self.cow.commit_transaction(tx)
        for f in modified:
            self.audit_logger.record(action="commit", target=f, decision="allow", detail="Committed CoW edit")
        return modified

    def discard_transaction(self, tx: CoWTransaction) -> None:
        self.cow.discard_transaction(tx)
        self.audit_logger.record(action="discard", target=tx.transaction_id, decision="allow", detail="Discarded CoW transaction")
