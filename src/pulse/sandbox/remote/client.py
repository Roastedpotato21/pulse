"""Remote Sandbox Client.

Implements the client side of the authenticated WebSocket protocol.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import websockets

from pulse.sandbox.remote.models import (
    ExecutionResultModel,
    SubmitExecutionRequest,
    SubmitExecutionResponse,
)
from pulse.sandbox.remote.protocol import RemoteSandboxClient

logger = logging.getLogger(__name__)


class RemoteClient(RemoteSandboxClient):
    """Client for the Remote Sandbox Server."""

    def __init__(self, endpoint_url: str, auth_token: str) -> None:
        self.endpoint_url = endpoint_url
        self.auth_token = auth_token
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._results: dict[str, asyncio.Future[ExecutionResultModel]] = {}
        self._responses: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._queues: dict[str, asyncio.Queue[tuple[str, str] | None]] = {}
        self._listener_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        """Establish the WebSocket connection."""
        if self._ws and not self._ws.closed:
            return
            
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        
        import os
        import ssl
        ssl_context = None
        
        tls_cert = os.environ.get("PULSE_TLS_CERT")
        tls_key = os.environ.get("PULSE_TLS_KEY")
        tls_ca = os.environ.get("PULSE_TLS_CA")
        
        if self.endpoint_url.startswith("wss://"):
            if not (tls_cert and tls_key and tls_ca):
                raise ValueError("mTLS certificates (PULSE_TLS_CERT, PULSE_TLS_KEY, PULSE_TLS_CA) are required for wss://")
            try:
                ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=tls_ca)
                ssl_context.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
            except (ssl.SSLError, OSError) as e:
                raise RuntimeError(f"Failed to load mTLS certificates: {e}")
        elif not ("127.0.0.1" in self.endpoint_url or "localhost" in self.endpoint_url or "[::1]" in self.endpoint_url):
            raise RuntimeError("Insecure ws:// connections are only allowed for loopback (127.0.0.1).")
                
        self._ws = await websockets.connect(
            self.endpoint_url, 
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
            ssl=ssl_context
        )
        self._listener_task = asyncio.create_task(self._listen())

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._listener_task:
            self._listener_task.cancel()

    async def _listen(self) -> None:
        """Background task to listen for messages from the server."""
        if not self._ws:
            return

        try:
            async for message in self._ws:
                try:
                    data = json.loads(message)
                    msg_type = data.get("type")
                    payload = data.get("payload", {})

                    if msg_type == "result":
                        result = ExecutionResultModel.from_dict(payload)
                        fut = self._results.get(result.execution_id)
                        if fut and not fut.done():
                            fut.set_result(result)
                        
                        queue = self._queues.get(result.execution_id)
                        if queue:
                            await queue.put(None)
                            
                    elif msg_type == "response":
                        # We use the execution_id if available, or just resolve all pending responses.
                        # For a single connection, we can simplify this.
                        # We'll just grab the first pending response future if no execution_id.
                        eid = payload.get("execution_id")
                        if eid and eid in self._responses:
                            fut = self._responses[eid]
                            if not fut.done():
                                fut.set_result(payload)
                        else:
                            for fut in self._responses.values():
                                if not fut.done():
                                    fut.set_result(payload)
                                    break

                    elif msg_type == "stream":
                        stream_data = payload.get("data", "")
                        stream_type = payload.get("stream", "stdout")
                        for queue in self._queues.values():
                            await queue.put((stream_type, stream_data))
                            
                    elif msg_type == "error":
                        logger.error(f"Remote server error: {payload}")
                        
                except (KeyError, ValueError, RuntimeError) as e:
                    logger.error(f"Error processing server message: {e}")
        except websockets.exceptions.ConnectionClosed:
            logger.info("Connection to remote server closed.")
            for fut in self._results.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("Connection closed"))
            for fut in self._responses.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("Connection closed"))
            for queue in self._queues.values():
                await queue.put(None)

    async def submit(self, request: SubmitExecutionRequest) -> SubmitExecutionResponse:
        """Submit an execution request to the remote worker."""
        await self.connect()
        
        self._results[request.execution_id] = asyncio.Future()
        self._responses[request.execution_id] = asyncio.Future()
        self._queues[request.execution_id] = asyncio.Queue()
        
        await self._ws.send(json.dumps({
            "action": "submit",
            "payload": request.to_dict()
        }))
        
        resp = await self._responses[request.execution_id]
        return SubmitExecutionResponse(execution_id=request.execution_id, status=resp.get("status", "STARTING"))
        
    async def cancel(self, execution_id: str) -> None:
        """Cancel an ongoing execution on the remote worker."""
        if not self._ws or self._ws.closed:
            return
            
        await self._ws.send(json.dumps({
            "action": "cancel",
            "payload": {"execution_id": execution_id}
        }))
        
    async def get_result(self, execution_id: str) -> ExecutionResultModel:
        """Wait for and retrieve the final execution result."""
        fut = self._results.get(execution_id)
        if not fut:
            raise ValueError(f"No active execution found for {execution_id}")
            
        try:
            return await fut
        finally:
            self._results.pop(execution_id, None)
            
    async def stream_output(self, execution_id: str) -> AsyncGenerator[tuple[str, str], None]:
        """Stream stdout and stderr from the remote execution."""
        queue = self._queues.get(execution_id)
        if not queue:
            return
            
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            self._queues.pop(execution_id, None)
            
    async def reconcile(self) -> None:
        """Reconcile orphaned or stale executions with the remote worker."""
        await self.connect()
        fut = asyncio.Future()
        temp_key = f"reconcile_{id(self)}"
        self._responses[temp_key] = fut
        try:
            await self._ws.send(json.dumps({"action": "reconcile", "payload": {}}))
            await fut
        finally:
            self._responses.pop(temp_key, None)
        
    async def upload_artifact(self, execution_id: str, archive_data: bytes) -> None:
        """Upload a workspace snapshot to the remote worker before execution."""
        import base64
        await self.connect()
        fut = asyncio.Future()
        # Use a temporary key for the response
        temp_key = f"upload_{execution_id}"
        self._responses[temp_key] = fut
        try:
            await self._ws.send(json.dumps({
                "action": "upload_artifact",
                "payload": {
                    "execution_id": execution_id,
                    "data": base64.b64encode(archive_data).decode("ascii")
                }
            }))
            await fut
        finally:
            self._responses.pop(temp_key, None)
        
    async def download_artifact(self, execution_id: str) -> bytes:
        """Download the modified workspace overlay from the remote worker after execution."""
        import base64
        await self.connect()
        fut = asyncio.Future()
        temp_key = f"download_{execution_id}"
        self._responses[temp_key] = fut
        try:
            await self._ws.send(json.dumps({
                "action": "download_artifact",
                "payload": {"execution_id": execution_id}
            }))
            resp = await fut
            if resp.get("status") == "DOWNLOADED" and "data" in resp:
                return base64.b64decode(resp["data"])
            return b""
        finally:
            self._responses.pop(temp_key, None)
