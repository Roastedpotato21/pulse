"""Remote Sandbox Protocol definition."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Protocol

from pulse.sandbox.remote.models import (
    ExecutionResultModel,
    SubmitExecutionRequest,
    SubmitExecutionResponse,
)


class RemoteSandboxClient(Protocol):
    """Protocol for communicating with a Remote Sandbox Worker.
    
    The implementation must support:
    - Authenticated transport
    - Artifact snapshot staging and retrieval
    - Execution submission
    - Execution cancellation
    - Execution reconciliation
    """
    
    async def submit(self, request: SubmitExecutionRequest) -> SubmitExecutionResponse:
        """Submit an execution request to the remote worker."""
        ...
        
    async def cancel(self, execution_id: str) -> None:
        """Cancel an ongoing execution on the remote worker."""
        ...
        
    async def get_result(self, execution_id: str) -> ExecutionResultModel:
        """Wait for and retrieve the final execution result."""
        ...
        
    async def stream_output(self, execution_id: str) -> AsyncGenerator[tuple[str, str], None]:
        """Stream stdout and stderr from the remote execution.
        
        Yields tuples of (stdout_chunk, stderr_chunk).
        """
        ...
        
    async def reconcile(self) -> None:
        """Reconcile orphaned or stale executions with the remote worker."""
        ...
        
    async def upload_artifact(self, execution_id: str, archive_data: bytes) -> None:
        """Upload a workspace snapshot to the remote worker before execution."""
        ...
        
    async def download_artifact(self, execution_id: str) -> bytes:
        """Download the modified workspace overlay from the remote worker after execution."""
        ...
