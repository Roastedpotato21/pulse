"""Data models for Remote Sandbox Protocol."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

REMOTE_PROTOCOL_VERSION = "1.0"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def validate_execution_id(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError("execution_id must be a safe 1-128 character identifier")
    return value


@dataclass
class SubmitExecutionRequest:
    protocol_version: str
    execution_id: str
    idempotency_key: str
    command: str | list[str]
    correlation_id: str | None = None
    working_directory: str | None = None
    env: dict[str, str] | None = None
    resource_policy_dict: dict[str, Any] | None = None
    network_policy_dict: dict[str, Any] | None = None
    secret_policy_dict: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.protocol_version != REMOTE_PROTOCOL_VERSION:
            raise ValueError("unsupported remote protocol version")
        validate_execution_id(self.execution_id)
        if not _SAFE_IDENTIFIER.fullmatch(self.idempotency_key):
            raise ValueError("idempotency_key must be a safe 1-128 character identifier")
        if isinstance(self.command, str):
            if not self.command or len(self.command) > 65_536 or "\x00" in self.command:
                raise ValueError("command must be 1-65536 characters without NUL bytes")
        elif isinstance(self.command, list):
            if not self.command or len(self.command) > 256 or any(
                not isinstance(item, str)
                or not item
                or len(item) > 65_536
                or "\x00" in item
                for item in self.command
            ):
                raise ValueError("command arguments are invalid or exceed protocol limits")
        else:
            raise TypeError("command must be a string or an array of strings")
        if self.working_directory:
            path = PurePosixPath(self.working_directory.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("working_directory must remain inside the execution workspace")
        if self.env is not None:
            if not isinstance(self.env, dict) or len(self.env) > 128:
                raise ValueError("env must be an object with at most 128 entries")
            if any(
                not isinstance(key, str)
                or not _SAFE_ENV_NAME.fullmatch(key)
                or not isinstance(value, str)
                or len(value) > 65_536
                or "\x00" in value
                for key, value in self.env.items()
            ):
                raise ValueError("env contains an invalid name or value")
        for policy in (
            self.resource_policy_dict,
            self.network_policy_dict,
            self.secret_policy_dict,
        ):
            if policy is not None and not isinstance(policy, dict):
                raise ValueError("execution policies must be objects")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "execution_id": self.execution_id,
            "idempotency_key": self.idempotency_key,
            "command": self.command,
            "correlation_id": self.correlation_id,
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
            correlation_id=data.get("correlation_id"),
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
