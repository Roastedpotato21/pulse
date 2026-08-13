"""Remote Sandbox Server.

Exposes the RemoteWorker over an authenticated WebSocket protocol.
"""

from __future__ import annotations

import asyncio
import http
import json
import logging
from typing import Any

import websockets

from pulse.sandbox.remote.models import (
    SubmitExecutionRequest,
    SubmitExecutionResponse,
)
from pulse.sandbox.remote.worker import RemoteWorker

logger = logging.getLogger(__name__)


class RemoteServer:
    """Provides the network boundary for the remote worker."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8080, auth_token: str = "dev-token", max_concurrent_executions: int = 10) -> None:
        self.host = host
        self.port = port
        # Support multiple tokens via comma-separated string if provided
        self.valid_tokens = set(auth_token.split(",")) if auth_token else {"dev-token"}
        self.max_concurrent_executions = max_concurrent_executions
        self.worker = RemoteWorker()
        self._active_executions: dict[str, asyncio.Task[Any]] = {}
        # Track tenant for each execution
        self._execution_tenants: dict[str, str] = {}

    async def _process_request(self, connection, request) -> Any | None:
        from websockets.http11 import Response
        """Enforce authenticated transport (R3)."""
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return Response(http.HTTPStatus.UNAUTHORIZED, "Unauthorized", websockets.Headers(), b"Unauthorized")
            
        token = auth_header.split(" ")[1]
        if token not in self.valid_tokens:
            logger.warning(f"Unauthorized connection attempt. Token: {token}")
            return Response(http.HTTPStatus.UNAUTHORIZED, "Unauthorized", websockets.Headers(), b"Unauthorized")
            
        return None

    async def start(self) -> None:
        """Start the WebSocket server."""
        await self.worker.initialize()
        
        # Phase 3: TLS Enforcement
        import ssl
        import os
        ssl_context = None
        if os.environ.get("PULSE_REMOTE_INSECURE") != "1":
            try:
                # In a real environment, this should load specific certs
                ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                # For development, you'd load a cert here, e.g., ssl_context.load_cert_chain(...)
            except Exception:
                logger.warning("Failed to create TLS context. Set PULSE_REMOTE_INSECURE=1 for dev.")
                
        async with websockets.serve(
            self._handle_client, 
            self.host, 
            self.port, 
            process_request=self._process_request,
            ping_interval=20,
            ping_timeout=20,
            max_size=1024 * 1024 * 50, # Enforce 50MB max payload size (R7)
            ssl=ssl_context
        ):
            scheme = "wss" if ssl_context else "ws"
            logger.info(f"Remote Sandbox Server listening on {scheme}://{self.host}:{self.port}")
            await asyncio.Future()  # run forever

    async def _handle_client(self, websocket: websockets.WebSocketServerProtocol) -> None:
        """Handle incoming client WebSocket connections."""
        
        async def send_output(stream: str, data: bytes) -> None:
            """Callback for streaming stdout/stderr (R4)."""
            try:
                await websocket.send(json.dumps({
                    "type": "stream",
                    "payload": {
                        "stream": stream,
                        "data": data.decode("utf-8", errors="replace")
                    }
                }))
            except websockets.exceptions.ConnectionClosed:
                pass

        async def run_execution(req: SubmitExecutionRequest, tenant_id: str) -> None:
            """Background task for running the execution."""
            try:
                result = await self.worker.execute_request(req, tenant_id=tenant_id, output_callback=send_output)
                try:
                    await websocket.send(json.dumps({
                        "type": "result", 
                        "payload": result.to_dict()
                    }))
                except websockets.exceptions.ConnectionClosed:
                    pass
            except Exception as e:
                logger.error(f"Execution failed: {e}")
                try:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "payload": f"Execution failed: {e}"
                    }))
                except websockets.exceptions.ConnectionClosed:
                    pass
            finally:
                self._active_executions.pop(req.execution_id, None)
                self._execution_tenants.pop(req.execution_id, None)

        client_executions: set[str] = set()
        
        # Derive tenant_id from auth header
        auth_header = websocket.request.headers.get("Authorization", "")
        tenant_id = auth_header.split(" ")[1] if auth_header.startswith("Bearer ") else "unknown"
        import hashlib
        tenant_hash = hashlib.sha256(tenant_id.encode()).hexdigest()[:12]

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    action = data.get("action")
                    payload = data.get("payload", {})
                    
                    if action == "submit":
                        if len(self._active_executions) >= self.max_concurrent_executions:
                            logger.warning("Concurrency limit reached, rejecting execution.")
                            await websocket.send(json.dumps({
                                "type": "error",
                                "payload": "Server is at maximum capacity, please try again later."
                            }))
                            continue
                            
                        req = SubmitExecutionRequest.from_dict(payload)
                        # Acknowledge submission
                        ack = SubmitExecutionResponse(execution_id=req.execution_id, status="STARTING")
                        await websocket.send(json.dumps({"type": "response", "payload": ack.to_dict()}))
                        
                        # Execute in background for streaming (R4)
                        task = asyncio.create_task(run_execution(req, tenant_hash))
                        self._active_executions[req.execution_id] = task
                        self._execution_tenants[req.execution_id] = tenant_hash
                        client_executions.add(req.execution_id)
                        
                    elif action == "cancel":
                        execution_id = payload.get("execution_id")
                        if execution_id:
                            # Cross-tenant prevention
                            if self._execution_tenants.get(execution_id) != tenant_hash:
                                await websocket.send(json.dumps({"type": "error", "payload": "Unauthorized"}))
                                continue
                            
                            # Cancel the worker execution
                            await self.worker.cancel(execution_id)
                            # The task will complete or raise CancelledError
                            task = self._active_executions.get(execution_id)
                            if task and not task.done():
                                task.cancel()
                            
                            await websocket.send(json.dumps({
                                "type": "response", 
                                "payload": {"status": "CANCELLED"}
                            }))
                            
                    elif action == "upload_artifact":
                        import base64
                        import tarfile
                        import io
                        execution_id = payload.get("execution_id")
                        b64_data = payload.get("data")
                        if execution_id and b64_data:
                            try:
                                data = base64.b64decode(b64_data)
                                workspace = self.worker.workspace_base_path / tenant_hash / execution_id
                                workspace.mkdir(parents=True, exist_ok=True)
                                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                                    import os
                                    for member in tar.getmembers():
                                        if member.issym() or member.islnk():
                                            raise Exception("Symlinks are not allowed in remote artifacts")
                                        member_path = os.path.join(str(workspace), member.name)
                                        if not os.path.abspath(member_path).startswith(os.path.abspath(str(workspace))):
                                            raise Exception("Path traversal detected in upload_artifact")
                                    # use data filter if available, else extractall (we checked manually above)
                                    if hasattr(tarfile, 'data_filter'):
                                        tar.extractall(path=workspace, filter='data')
                                    else:
                                        tar.extractall(path=workspace)
                                await websocket.send(json.dumps({
                                    "type": "response", 
                                    "payload": {"status": "UPLOADED"}
                                }))
                            except Exception as e:
                                logger.error(f"Failed to extract artifact: {e}")
                                await websocket.send(json.dumps({"type": "error", "payload": f"Upload failed: {e}"}))
                    
                    elif action == "download_artifact":
                        import base64
                        import tarfile
                        import io
                        execution_id = payload.get("execution_id")
                        if execution_id:
                            overlay_path = self.worker.get_overlay_path(execution_id)
                            if not overlay_path or not overlay_path.exists():
                                await websocket.send(json.dumps({
                                    "type": "response", 
                                    "payload": {"status": "NO_ARTIFACT"}
                                }))
                            else:
                                bio = io.BytesIO()
                                with tarfile.open(fileobj=bio, mode="w:gz") as tar:
                                    for item in overlay_path.iterdir():
                                        tar.add(item, arcname=item.name)
                                b64_out = base64.b64encode(bio.getvalue()).decode("ascii")
                                await websocket.send(json.dumps({
                                    "type": "response", 
                                    "payload": {"status": "DOWNLOADED", "data": b64_out}
                                }))
                                # Cleanup overlay after download
                                self.worker.cleanup_overlay(execution_id)
                                
                    elif action == "reconcile":
                        # Clean up orphaned workspaces not matching active executions for this tenant
                        orphans = 0
                        tenant_workspace_path = self.worker.workspace_base_path / tenant_hash
                        if tenant_workspace_path.exists():
                            for ws in tenant_workspace_path.iterdir():
                                if ws.is_dir() and ws.name not in self._active_executions:
                                    self.worker.cleanup_workspace(tenant_hash, ws.name)
                                    orphans += 1
                        await websocket.send(json.dumps({
                            "type": "response",
                            "payload": {"status": "RECONCILED", "orphans_cleaned": orphans}
                        }))
                                
                    else:
                        await websocket.send(json.dumps({
                            "type": "error", 
                            "payload": f"Unknown action: {action}"
                        }))
                        
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    await websocket.send(json.dumps({"type": "error", "payload": str(e)}))
        except websockets.exceptions.ConnectionClosed:
            logger.info("Client disconnected.")
        finally:
            # R5: Lifecycle + heartbeat + recovery (cleanup on disconnect)
            for eid in client_executions:
                task = self._active_executions.get(eid)
                if task and not task.done():
                    logger.info(f"Cancelling execution {eid} due to client disconnect.")
                    task.cancel()
                    # Also tell the worker to cancel
                    asyncio.create_task(self.worker.cancel(eid))
                self.worker.cleanup_workspace(tenant_hash, eid)
                self._execution_tenants.pop(eid, None)

def main() -> None:
    logging.basicConfig(level=logging.INFO)
    import os
    host = os.environ.get("PULSE_REMOTE_HOST", "127.0.0.1")
    port = int(os.environ.get("PULSE_REMOTE_PORT", "8080"))
    token = os.environ.get("PULSE_REMOTE_TOKEN", "dev-token")
    server = RemoteServer(host=host, port=port, auth_token=token)
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Server shutting down.")

if __name__ == "__main__":
    main()
