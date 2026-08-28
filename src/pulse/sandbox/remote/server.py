"""Remote Sandbox Server.

Exposes the RemoteWorker over an authenticated WebSocket protocol.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hmac
import http
import json
import logging
import os
import sqlite3
from contextlib import suppress
from pathlib import Path
from typing import Any

import websockets

from pulse import __version__
from pulse.production import is_secure_remote_token
from pulse.sandbox.remote.models import (
    SubmitExecutionRequest,
    SubmitExecutionResponse,
)
from pulse.sandbox.remote.worker import RemoteWorker
from pulse.storage import migrate_database

REMOTE_EXECUTION_SCHEMA_VERSION = 2

logger = logging.getLogger(__name__)


def _append_log_entry(path: Path, entry: str) -> None:
    """Append a stream entry off the event loop."""
    with path.open("a", encoding="utf-8") as stream_log:
        stream_log.write(entry)


def _read_log_entries(path: Path) -> list[str]:
    """Read complete historical stream entries off the event loop."""
    with path.open("r", encoding="utf-8") as stream_log:
        return [line.strip() for line in stream_log if line.strip()]


class RemoteExecutionStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        def migration(conn: sqlite3.Connection, _current: int) -> None:
            conn.execute("""CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    correlation_id TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(executions)").fetchall()
            }
            if "correlation_id" not in columns:
                conn.execute("ALTER TABLE executions ADD COLUMN correlation_id TEXT")

        migrate_database(self.db_path, REMOTE_EXECUTION_SCHEMA_VERSION, migration)

    def create(
        self,
        execution_id: str,
        tenant_id: str,
        correlation_id: str | None = None,
    ) -> None:
        now = datetime.datetime.now(datetime.UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO executions (execution_id, tenant_id, status, correlation_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(execution_id) DO NOTHING",
                (execution_id, tenant_id, "RUNNING", correlation_id, now, now),
            )

    def update(
        self, execution_id: str, status: str, result_json: dict[str, Any] | None = None
    ) -> None:
        now = datetime.datetime.now(datetime.UTC).isoformat()
        with self._connect() as conn:
            if result_json:
                conn.execute(
                    "UPDATE executions SET status = ?, result_json = ?, updated_at = ? WHERE execution_id = ?",
                    (status, json.dumps(result_json), now, execution_id),
                )
            else:
                conn.execute(
                    "UPDATE executions SET status = ?, updated_at = ? WHERE execution_id = ?",
                    (status, now, execution_id),
                )

    def get(self, execution_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, result_json, created_at, updated_at, correlation_id FROM executions WHERE execution_id = ? AND tenant_id = ?",
                (execution_id, tenant_id),
            ).fetchone()
            if not row:
                return None
            return {
                "status": row[0],
                "result": json.loads(row[1]) if row[1] else None,
                "created_at": row[2],
                "updated_at": row[3],
                "correlation_id": row[4],
            }

    def cleanup_old(self, max_age_hours: int = 1) -> list[tuple[str, str]]:
        threshold = (
            datetime.datetime.now(datetime.UTC)
            - datetime.timedelta(hours=max_age_hours)
        ).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT execution_id, tenant_id FROM executions WHERE status IN ('COMPLETED', 'FAILED') AND updated_at < ?",
                (threshold,),
            ).fetchall()
            for row in rows:
                conn.execute("DELETE FROM executions WHERE execution_id = ?", (row[0],))
            return rows

    def mark_interrupted_running_unknown(self) -> int:
        """Quarantine executions left RUNNING by a server restart.

        A fresh server process has no proof that the old container completed
        or that its side effects were not applied.  Reporting UNKNOWN makes
        callers reconcile deliberately instead of replaying the command.
        """
        now = datetime.datetime.now(datetime.UTC).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE executions SET status = 'UNKNOWN', updated_at = ? WHERE status = 'RUNNING'",
                (now,),
            )
            return cursor.rowcount


class RemoteServer:
    """Provides the network boundary for the remote worker."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        auth_token: str | None = None,
        max_concurrent_executions: int = 10,
        workspace_root: Path | None = None,
        database_path: Path | None = None,
        retention_hours: int = 24,
        production_mode: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        # Support multiple tokens via comma-separated string if provided
        if not auth_token:
            raise ValueError(
                "PULSE_REMOTE_TOKEN must be configured; insecure default tokens are disabled."
            )
        self.valid_tokens = {token.strip() for token in auth_token.split(",") if token.strip()}
        if production_mode and any(
            not is_secure_remote_token(token) for token in self.valid_tokens
        ):
            raise ValueError(
                "Every production PULSE_REMOTE_TOKEN must be random, non-placeholder, "
                "and at least 32 characters."
            )
        if not self.valid_tokens:
            raise ValueError("PULSE_REMOTE_TOKEN does not contain a usable token.")
        if not 1 <= max_concurrent_executions <= 128:
            raise ValueError("max_concurrent_executions must be between 1 and 128.")
        if not 1 <= retention_hours <= 8760:
            raise ValueError("retention_hours must be between 1 and 8760.")
        self.max_concurrent_executions = max_concurrent_executions
        self.retention_hours = retention_hours
        self.worker = RemoteWorker(workspace_root)
        self.store = RemoteExecutionStore(
            database_path or Path(".remote_sandbox.db")
        )
        self._ready = False
        self._ready_event = asyncio.Event()
        self._active_executions: dict[str, asyncio.Task[Any]] = {}
        # Track tenant for each execution
        self._execution_tenants: dict[str, str] = {}
        # Track attached clients per execution
        self._attached_websockets: dict[
            str, set[websockets.WebSocketServerProtocol]
        ] = {}

    async def _ttl_cleanup(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                old = self.store.cleanup_old(max_age_hours=self.retention_hours)
                for eid, tid in old:
                    self.worker.cleanup_workspace(tid, eid)
            except (OSError, RuntimeError) as e:
                logger.error(f"TTL cleanup failed: {e}")

    async def _process_request(self, connection, request) -> Any | None:
        from websockets.http11 import Response

        """Enforce authenticated transport (R3)."""
        if request.path in {"/healthz", "/readyz"}:
            ready = request.path == "/healthz" or self._ready
            status = http.HTTPStatus.OK if ready else http.HTTPStatus.SERVICE_UNAVAILABLE
            body = json.dumps(
                {"status": "ok" if ready else "not_ready"}, separators=(",", ":")
            ).encode("utf-8")
            return Response(status, status.phrase, websockets.Headers(), body)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return Response(
                http.HTTPStatus.UNAUTHORIZED,
                "Unauthorized",
                websockets.Headers(),
                b"Unauthorized",
            )

        token = auth_header.split(" ", 1)[1]
        if not any(hmac.compare_digest(token, valid) for valid in self.valid_tokens):
            logger.warning("Unauthorized remote sandbox connection attempt.")
            return Response(
                http.HTTPStatus.UNAUTHORIZED,
                "Unauthorized",
                websockets.Headers(),
                b"Unauthorized",
            )

        return None

    async def start(self) -> None:
        """Start the WebSocket server."""
        await self.worker.initialize()
        interrupted = self.store.mark_interrupted_running_unknown()
        if interrupted:
            logger.warning(
                "Marked %s interrupted remote executions as UNKNOWN.", interrupted
            )

        # Phase 3: TLS Enforcement
        import os
        import ssl

        ssl_context = None

        tls_cert = os.environ.get("PULSE_TLS_CERT")
        tls_key = os.environ.get("PULSE_TLS_KEY")
        tls_ca = os.environ.get("PULSE_TLS_CA")

        if tls_cert and tls_key and tls_ca:
            try:
                ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                ssl_context.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
                ssl_context.load_verify_locations(cafile=tls_ca)
                ssl_context.verify_mode = ssl.CERT_REQUIRED
            except (ssl.SSLError, OSError) as e:
                raise RuntimeError(f"Failed to load mTLS certificates: {e}")
        elif self.host in ("127.0.0.1", "localhost", "::1"):
            logger.warning("Starting in insecure loopback mode without mTLS.")
        else:
            raise RuntimeError(
                "mTLS certificates (PULSE_TLS_CERT, PULSE_TLS_KEY, PULSE_TLS_CA) "
                "are strictly required for non-loopback connections."
            )

        cleanup_task = asyncio.create_task(self._ttl_cleanup())
        try:
            async with websockets.serve(
                self._handle_client,
                self.host,
                self.port,
                process_request=self._process_request,
                ping_interval=20,
                ping_timeout=20,
                max_size=1024 * 1024 * 50,  # Enforce 50MB max payload size (R7)
                ssl=ssl_context,
            ) as websocket_server:
                sockets = websocket_server.sockets
                if sockets:
                    self.port = int(sockets[0].getsockname()[1])
                self._ready = True
                self._ready_event.set()
                scheme = "wss" if ssl_context else "ws"
                logger.info(
                    "Remote Sandbox Server listening on %s://%s:%s",
                    scheme,
                    self.host,
                    self.port,
                )
                await asyncio.Future()  # run forever
        finally:
            self._ready = False
            self._ready_event.clear()
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task

    async def wait_until_ready(self, timeout: float = 5.0) -> None:
        """Wait until the listening socket is bound and accepting connections."""
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    async def _handle_client(
        self, websocket: websockets.WebSocketServerProtocol
    ) -> None:
        """Handle incoming client WebSocket connections."""

        # Derive tenant_id from auth header
        auth_header = websocket.request.headers.get("Authorization", "")
        tenant_id = (
            auth_header.split(" ")[1]
            if auth_header.startswith("Bearer ")
            else "unknown"
        )
        import hashlib

        tenant_hash = hashlib.sha256(tenant_id.encode()).hexdigest()[:12]

        async def run_execution(req: SubmitExecutionRequest, tenant_id: str) -> None:
            """Background task for running the execution."""

            async def scoped_send_output(stream: str, data: bytes) -> None:
                log_entry = (
                    json.dumps(
                        {
                            "type": "stream",
                            "payload": {
                                "execution_id": req.execution_id,
                                "stream": stream,
                                "data": data.decode("utf-8", errors="replace"),
                            },
                        }
                    )
                    + "\n"
                )

                log_file = (
                    self.worker.workspace_base_path
                    / tenant_id
                    / req.execution_id
                    / ".pulse_stream.log"
                )
                try:
                    if log_file.parent.exists():
                        await asyncio.to_thread(_append_log_entry, log_file, log_entry)
                except OSError:
                    logger.warning(
                        "Unable to persist remote stream for %s.", req.execution_id
                    )

                attached = self._attached_websockets.get(req.execution_id, set())
                dead_ws = set()
                for ws in attached:
                    try:
                        await ws.send(log_entry)
                    except websockets.exceptions.ConnectionClosed:
                        dead_ws.add(ws)
                for ws in dead_ws:
                    attached.discard(ws)

            try:
                self.store.create(
                    req.execution_id, tenant_id, correlation_id=req.correlation_id
                )
                result = await self.worker.execute_request(
                    req, tenant_id=tenant_id, output_callback=scoped_send_output
                )
                self.store.update(req.execution_id, "COMPLETED", result.to_dict())

                res_msg = json.dumps({"type": "result", "payload": result.to_dict()})
                attached = self._attached_websockets.get(req.execution_id, set())
                dead_ws = set()
                for ws in attached:
                    try:
                        await ws.send(res_msg)
                    except websockets.exceptions.ConnectionClosed:
                        dead_ws.add(ws)
                for ws in dead_ws:
                    attached.discard(ws)
            except (OSError, RuntimeError) as e:
                logger.error(f"Execution failed: {e}")
                err_msg = f"Execution failed: {e}"
                from pulse.sandbox.process import ProcessResult

                fallback_res = ProcessResult(
                    command=req.command,
                    exit_code=-1,
                    stdout="",
                    stderr=err_msg,
                    duration_ms=0,
                    timed_out=False,
                    truncated=False,
                    pid=None,
                    overlay_path=None,
                    termination_reason="error",
                )
                self.store.update(req.execution_id, "FAILED", fallback_res.to_dict())

                err_json = json.dumps(
                    {"type": "result", "payload": fallback_res.to_dict()}
                )
                attached = self._attached_websockets.get(req.execution_id, set())
                dead_ws = set()
                for ws in attached:
                    try:
                        await ws.send(err_json)
                    except websockets.exceptions.ConnectionClosed:
                        dead_ws.add(ws)
                for ws in dead_ws:
                    attached.discard(ws)
            finally:
                self._active_executions.pop(req.execution_id, None)
                self._execution_tenants.pop(req.execution_id, None)
                self._attached_websockets.pop(req.execution_id, None)

        client_executions: set[str] = set()

        try:
            async for message in websocket:
                request_id: str | None = None
                try:
                    data = json.loads(message)
                    action = data.get("action")
                    payload = data.get("payload", {})
                    request_id = payload.get("request_id")

                    if action == "submit":
                        if (
                            len(self._active_executions)
                            >= self.max_concurrent_executions
                        ):
                            logger.warning(
                                "Concurrency limit reached, rejecting execution."
                            )
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "error",
                                        "payload": {
                                            "message": "Server is at maximum capacity, please try again later.",
                                            "request_id": request_id,
                                        },
                                    }
                                )
                            )
                            continue

                        req = SubmitExecutionRequest.from_dict(payload)
                        # Acknowledge submission
                        ack = SubmitExecutionResponse(
                            execution_id=req.execution_id, status="STARTING"
                        )
                        ack_payload = ack.to_dict() | {"request_id": request_id}
                        await websocket.send(
                            json.dumps({"type": "response", "payload": ack_payload})
                        )

                        # Execute in background for streaming (R4)
                        task = asyncio.create_task(run_execution(req, tenant_hash))
                        self._active_executions[req.execution_id] = task
                        self._execution_tenants[req.execution_id] = tenant_hash
                        if req.execution_id not in self._attached_websockets:
                            self._attached_websockets[req.execution_id] = set()
                        self._attached_websockets[req.execution_id].add(websocket)
                        client_executions.add(req.execution_id)

                    elif action == "cancel":
                        execution_id = payload.get("execution_id")
                        if execution_id:
                            # Cross-tenant prevention
                            if self._execution_tenants.get(execution_id) != tenant_hash:
                                await websocket.send(
                                    json.dumps(
                                        {"type": "error", "payload": "Unauthorized"}
                                    )
                                )
                                continue

                            # Cancel the worker execution
                            await self.worker.cancel(execution_id)
                            # The task will complete or raise CancelledError
                            task = self._active_executions.get(execution_id)
                            if task and not task.done():
                                task.cancel()

                            # Update store explicitly
                            self.store.update(
                                execution_id, "FAILED", {"error": "CANCELLED"}
                            )

                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "response",
                                        "payload": {
                                            "status": "CANCELLED",
                                            "request_id": request_id,
                                        },
                                    }
                                )
                            )

                    elif action == "upload_artifact":
                        import base64
                        import io
                        import tarfile

                        execution_id = payload.get("execution_id")
                        b64_data = payload.get("data")
                        if execution_id and b64_data:
                            try:
                                data = base64.b64decode(b64_data)
                                workspace = (
                                    self.worker.workspace_base_path
                                    / tenant_hash
                                    / execution_id
                                )
                                workspace.mkdir(parents=True, exist_ok=True)
                                with tarfile.open(
                                    fileobj=io.BytesIO(data), mode="r:gz"
                                ) as tar:
                                    import os

                                    from pulse.sandbox.errors import (
                                        SandboxSecurityError,
                                    )

                                    for member in tar.getmembers():
                                        if member.issym() or member.islnk():
                                            raise SandboxSecurityError(
                                                "Symlinks are not allowed in remote artifacts"
                                            )
                                        member_path = os.path.join(
                                            str(workspace), member.name
                                        )
                                        if not os.path.abspath(member_path).startswith(
                                            os.path.abspath(str(workspace))
                                        ):
                                            raise SandboxSecurityError(
                                                "Path traversal detected in upload_artifact"
                                            )
                                    # use data filter if available, else extractall (we checked manually above)
                                    if hasattr(tarfile, "data_filter"):
                                        tar.extractall(path=workspace, filter="data")
                                    else:
                                        tar.extractall(path=workspace)
                                await websocket.send(
                                    json.dumps(
                                        {
                                            "type": "response",
                                            "payload": {
                                                "status": "UPLOADED",
                                                "request_id": request_id,
                                            },
                                        }
                                    )
                                )
                            except (OSError, ValueError, RuntimeError) as e:
                                logger.error(f"Failed to extract artifact: {e}")
                                await websocket.send(
                                    json.dumps(
                                        {
                                            "type": "error",
                                            "payload": {
                                                "message": f"Upload failed: {e}",
                                                "request_id": request_id,
                                            },
                                        }
                                    )
                                )

                    elif action == "download_artifact":
                        import base64
                        import io
                        import tarfile

                        execution_id = payload.get("execution_id")
                        if execution_id:
                            if not self.store.get(execution_id, tenant_hash):
                                await websocket.send(
                                    json.dumps(
                                        {
                                            "type": "error",
                                            "payload": {
                                                "message": "NOT_FOUND",
                                                "request_id": request_id,
                                            },
                                        }
                                    )
                                )
                                continue
                            overlay_path = self.worker.get_overlay_path(execution_id)
                            if not overlay_path or not overlay_path.exists():
                                await websocket.send(
                                    json.dumps(
                                        {
                                            "type": "response",
                                            "payload": {
                                                "status": "NO_ARTIFACT",
                                                "request_id": request_id,
                                            },
                                        }
                                    )
                                )
                            else:
                                bio = io.BytesIO()
                                with tarfile.open(fileobj=bio, mode="w:gz") as tar:
                                    for item in overlay_path.iterdir():
                                        tar.add(item, arcname=item.name)
                                b64_out = base64.b64encode(bio.getvalue()).decode(
                                    "ascii"
                                )
                                await websocket.send(
                                    json.dumps(
                                        {
                                            "type": "response",
                                            "payload": {
                                                "status": "DOWNLOADED",
                                                "data": b64_out,
                                                "request_id": request_id,
                                            },
                                        }
                                    )
                                )
                                # Cleanup overlay after download
                                self.worker.cleanup_overlay(execution_id)

                    elif action == "reconcile":
                        # Clean up orphaned workspaces not matching active executions for this tenant
                        orphans = 0
                        tenant_workspace_path = (
                            self.worker.workspace_base_path / tenant_hash
                        )
                        if tenant_workspace_path.exists():
                            for ws in tenant_workspace_path.iterdir():
                                if (
                                    ws.is_dir()
                                    and ws.name not in self._active_executions
                                ):
                                    self.worker.cleanup_workspace(tenant_hash, ws.name)
                                    orphans += 1
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "response",
                                    "payload": {
                                        "status": "RECONCILED",
                                        "orphans_cleaned": orphans,
                                        "request_id": request_id,
                                    },
                                }
                            )
                        )

                    elif action == "status":
                        execution_id = payload.get("execution_id")
                        request_id = payload.get("request_id")
                        state = self.store.get(execution_id, tenant_hash)
                        if not state:
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "response",
                                        "payload": {
                                            "status": "NOT_FOUND",
                                            "request_id": request_id,
                                        },
                                    }
                                )
                            )
                        elif (
                            state["status"] in {"COMPLETED", "FAILED"}
                            and state["result"]
                        ):
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "response",
                                        "payload": {
                                            "status": state["status"],
                                            "request_id": request_id,
                                        },
                                    }
                                )
                            )

                    elif action == "attach":
                        execution_id = payload.get("execution_id")
                        state = self.store.get(execution_id, tenant_hash)
                        if not state:
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "error",
                                        "payload": {
                                            "message": "NOT_FOUND",
                                            "request_id": request_id,
                                        },
                                    }
                                )
                            )
                            continue

                        # Dump historical logs
                        log_file = (
                            self.worker.workspace_base_path
                            / tenant_hash
                            / execution_id
                            / ".pulse_stream.log"
                        )
                        if log_file.exists():
                            try:
                                for line in await asyncio.to_thread(
                                    _read_log_entries, log_file
                                ):
                                    await websocket.send(line)
                            except OSError:
                                logger.warning(
                                    "Unable to read remote stream history for %s.",
                                    execution_id,
                                )

                        if state["status"] == "RUNNING":
                            if execution_id not in self._attached_websockets:
                                self._attached_websockets[execution_id] = set()
                            if (
                                len(self._attached_websockets[execution_id]) > 0
                                and websocket
                                not in self._attached_websockets[execution_id]
                            ):
                                await websocket.send(
                                    json.dumps(
                                        {
                                            "type": "error",
                                            "payload": {
                                                "message": "Another client is already attached",
                                                "request_id": request_id,
                                            },
                                        }
                                    )
                                )
                                continue

                            self._attached_websockets[execution_id].add(websocket)
                            client_executions.add(execution_id)
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "response",
                                        "payload": {
                                            "status": "ATTACHED",
                                            "request_id": request_id,
                                        },
                                    }
                                )
                            )
                        elif (
                            state["status"] in {"COMPLETED", "FAILED"}
                            and state["result"]
                        ):
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "response",
                                        "payload": {
                                            "status": state["status"],
                                            "request_id": request_id,
                                        },
                                    }
                                )
                            )
                            await websocket.send(
                                json.dumps(
                                    {"type": "result", "payload": state["result"]}
                                )
                            )
                        else:
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "response",
                                        "payload": {
                                            "status": state["status"],
                                            "request_id": request_id,
                                        },
                                    }
                                )
                            )

                    else:
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "error",
                                    "payload": {
                                        "message": f"Unknown action: {action}",
                                        "request_id": request_id,
                                    },
                                }
                            )
                        )

                except (KeyError, ValueError, OSError, RuntimeError) as e:
                    logger.error(f"Error processing message: {e}")
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "error",
                                "payload": {
                                    "message": str(e),
                                    "request_id": request_id,
                                },
                            }
                        )
                    )
        except websockets.exceptions.ConnectionClosed:
            logger.info("Client disconnected.")
        finally:
            # R5/GAP-07: Detach client but do NOT cancel execution
            for eid in client_executions:
                if eid in self._attached_websockets:
                    self._attached_websockets[eid].discard(websocket)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pulse-remote",
        description="Run Pulse's authenticated remote sandbox worker.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--host",
        default=os.environ.get("PULSE_REMOTE_HOST", "127.0.0.1"),
        help="Bind host (default: PULSE_REMOTE_HOST or 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        default=os.environ.get("PULSE_REMOTE_PORT", "8080"),
        type=int,
        help="Bind port (default: PULSE_REMOTE_PORT or 8080).",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(
            os.environ.get("PULSE_REMOTE_WORKSPACE_ROOT", ".pulse/remote-workspaces")
        ),
        help="Tenant workspace root (default: PULSE_REMOTE_WORKSPACE_ROOT).",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(
            os.environ.get("PULSE_REMOTE_DB", ".pulse/remote-executions.sqlite3")
        ),
        help="Durable execution database (default: PULSE_REMOTE_DB).",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=os.environ.get("PULSE_REMOTE_MAX_CONCURRENCY", "10"),
        help="Maximum active executions (1-128).",
    )
    parser.add_argument(
        "--retention-hours",
        type=int,
        default=os.environ.get("PULSE_REMOTE_RETENTION_HOURS", "24"),
        help="Retention for terminal execution records (1-8760 hours).",
    )
    parser.add_argument(
        "--development",
        action="store_true",
        help="Allow short development tokens; only appropriate on loopback.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("PULSE_REMOTE_TOKEN")
    if args.development and args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("--development is restricted to a loopback host")
    if not args.development:
        if not args.workspace_root.is_absolute() or not args.database.is_absolute():
            parser.error(
                "production remote paths must be absolute; set "
                "PULSE_REMOTE_WORKSPACE_ROOT and PULSE_REMOTE_DB"
            )
        if args.host not in {"127.0.0.1", "localhost", "::1"}:
            for variable in ("PULSE_TLS_CERT", "PULSE_TLS_KEY", "PULSE_TLS_CA"):
                value = os.environ.get(variable, "")
                path = Path(value) if value else None
                if not path or not path.is_absolute() or not path.is_file():
                    parser.error(
                        f"{variable} must reference an existing absolute file "
                        "for a non-loopback worker"
                    )
    server = RemoteServer(
        host=args.host,
        port=args.port,
        auth_token=token,
        max_concurrent_executions=args.max_concurrency,
        workspace_root=args.workspace_root,
        database_path=args.database,
        retention_hours=args.retention_hours,
        production_mode=not args.development,
    )
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Server shutting down.")


if __name__ == "__main__":
    main()
