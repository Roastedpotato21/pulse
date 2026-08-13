"""Data models for Remote Sandbox Protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SubmitExecutionRequest:
    protocol_version: str
    execution_id: str
    idempotency_key: str
    command: str | list[str]
    working_directory: str | None = None
    env: dict[str, str] | None = None
    resource_policy_dict: dict[str, Any] | None = None
    network_policy_dict: dict[str, Any] | None = None
    secret_policy_dict: dict[str, Any] | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "execution_id": self.execution_id,
            "idempotency_key": self.idempotency_key,
            "command": self.command,
            "working_directory": self.working_directory,
            "env": self.env,
            "resource_policy_dict": self.resource_policy_dict,
            "network_policy_dict": self.network_policy_dict,
            "secret_policy_dict": self.secret_policy_dict,
        }
        
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubmitExecutionRequest:
        return cls(
            protocol_version=data["protocol_version"],
            execution_id=data["execution_id"],
            idempotency_key=data["idempotency_key"],
            command=data["command"],
            working_directory=data.get("working_directory"),
            env=data.get("env"),
            resource_policy_dict=data.get("resource_policy_dict"),
            network_policy_dict=data.get("network_policy_dict"),
            secret_policy_dict=data.get("secret_policy_dict"),
        )


@dataclass
class SubmitExecutionResponse:
    execution_id: str
    status: str
    error: str | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "error": self.error,
        }
        
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubmitExecutionResponse:
        return cls(
            execution_id=data["execution_id"],
            status=data["status"],
            error=data.get("error"),
        )


@dataclass
class ExecutionResultModel:
    execution_id: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    truncated: bool = False
    termination_reason: str | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "termination_reason": self.termination_reason,
        }
        
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionResultModel:
        return cls(
            execution_id=data["execution_id"],
            command=data["command"],
            exit_code=data["exit_code"],
            stdout=data["stdout"],
            stderr=data["stderr"],
            duration_ms=data["duration_ms"],
            timed_out=data.get("timed_out", False),
            truncated=data.get("truncated", False),
            termination_reason=data.get("termination_reason"),
        )
