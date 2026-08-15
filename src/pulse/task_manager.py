"""Production-grade Task Manager for Pulse.

Provides task creation, priority queuing, pausing, resuming, canceling,
checkpointing, persistent state storage across sessions, VS Code UI event emissions,
telemetry/memory integrations, and priority-scheduled concurrent execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & Data Models
# ---------------------------------------------------------------------------


class TaskConcurrencyError(Exception):
    """Raised when a stale task update is rejected via OCC."""
    def __init__(self, task_id: str):
        super().__init__(f"Task {task_id} was modified by another process. Stale update rejected.")
        self.task_id = task_id


class StaleWorkerError(Exception):
    """Raised when a worker attempts to modify a task it no longer owns."""
    def __init__(self, task_id: str):
        super().__init__(f"Worker no longer owns task {task_id}.")
        self.task_id = task_id


class LeaseLostError(Exception):
    """Raised when a heartbeat detects that task ownership has been lost.

    This exception is used by the execution supervisor to cancel the
    running worker_func when the lease can no longer be renewed.
    It is distinct from StaleWorkerError (which guards individual mutations)
    because it signals that the *entire execution* must terminate.

    NOTE: asyncio.Task.cancel() delivers CancelledError at the next await
    point.  It cannot terminate an already-issued HTTP request, LLM call,
    or spawned shell process.  This mechanism stops the Pulse worker loop
    from progressing after lease loss but does not provide universal
    transactional rollback of external effects.  Remote execution
    reconciliation remains GAP-07 scope.
    """
    def __init__(self, task_id: str):
        super().__init__(f"Lease lost for task {task_id}. Execution must terminate.")
        self.task_id = task_id


class TaskStatus(Enum):
    """Current state of a managed task."""

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECOVERY_PENDING = "RECOVERY_PENDING"


class TaskPriority(Enum):
    """Priority level for task scheduling."""

    LOW = 10
    MEDIUM = 20
    HIGH = 30
    CRITICAL = 40

    @classmethod
    def from_str(cls, val: str) -> TaskPriority:
        val_upper = val.upper().strip()
        for p in cls:
            if p.name == val_upper:
                return p
        return cls.MEDIUM


@dataclass(slots=True)
class TaskCheckpoint:
    """State snapshot for task resumption across sessions."""

    checkpoint_id: str
    task_id: str
    step_index: int
    state_data: dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = 1


@dataclass(slots=True)
class TaskExecutionRecord:
    """Audit record for a single step execution within a task."""

    timestamp: str
    action: str
    detail: str
    duration_ms: float = 0.0
    success: bool = True


@dataclass(slots=True)
class TaskEvent:
    """Structured event emitted for VS Code UI and RPC clients."""

    event_type: str
    task_id: str
    status: TaskStatus
    progress: float
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    """A unit of work managed by Pulse."""

    id: str
    title: str
    goal: str
    priority: TaskPriority = TaskPriority.MEDIUM
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0  # 0.0 to 100.0
    retries: int = 0
    max_retries: int = 3
    depends_on: list[str] = field(default_factory=list)
    checkpoints: list[TaskCheckpoint] = field(default_factory=list)
    history: list[TaskExecutionRecord] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    result: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    owner_id: str | None = None
    lease_expires_at: str | None = None
    lease_epoch: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize Task object to JSON-compatible dictionary."""
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "priority": self.priority.name,
            "status": self.status.value,
            "progress": self.progress,
            "retries": self.retries,
            "max_retries": self.max_retries,
            "depends_on": self.depends_on,
            "checkpoints": [asdict(cp) for cp in self.checkpoints],
            "history": [asdict(rec) for rec in self.history],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
            "metadata": self.metadata,
            "version": self.version,
            "owner_id": self.owner_id,
            "lease_expires_at": self.lease_expires_at,
            "lease_epoch": self.lease_epoch,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """Deserialize Task object from dictionary."""
        priority = TaskPriority.from_str(data.get("priority", "MEDIUM"))
        status = TaskStatus(data.get("status", "PENDING"))

        checkpoints = [
            TaskCheckpoint(**cp) for cp in data.get("checkpoints", []) if isinstance(cp, dict)
        ]
        history = [
            TaskExecutionRecord(**rec) for rec in data.get("history", []) if isinstance(rec, dict)
        ]

        return cls(
            id=data["id"],
            title=data.get("title", ""),
            goal=data.get("goal", ""),
            priority=priority,
            status=status,
            progress=float(data.get("progress", 0.0)),
            retries=int(data.get("retries", 0)),
            max_retries=int(data.get("max_retries", 3)),
            depends_on=list(data.get("depends_on", [])),
            checkpoints=checkpoints,
            history=history,
            created_at=data.get("created_at", datetime.now(UTC).isoformat()),
            updated_at=data.get("updated_at", datetime.now(UTC).isoformat()),
            result=data.get("result"),
            error=data.get("error"),
            metadata=data.get("metadata", {}),
            version=int(data.get("version", 1)),
            owner_id=data.get("owner_id"),
            lease_expires_at=data.get("lease_expires_at"),
            lease_epoch=int(data.get("lease_epoch", 0)),
        )


# ---------------------------------------------------------------------------
# Task Store & Event Bus
# ---------------------------------------------------------------------------


class TaskStore:
    """SQLite-backed persistence store for Tasks and Checkpoints."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()
        self.store_dir = self.workspace / ".pulse"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.store_file = self.store_dir / "tasks.sqlite3"
        self._ensure_schema()
        self._migrate_legacy_json()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.store_file, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL,
                    retries INTEGER NOT NULL,
                    max_retries INTEGER NOT NULL,
                    depends_on TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    metadata TEXT NOT NULL,
                    checkpoints TEXT NOT NULL,
                    history TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    owner_id TEXT,
                    lease_expires_at TEXT,
                    lease_epoch INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                """
            )
            # Safe schema migration for OCC
            cursor = conn.execute("PRAGMA table_info(tasks)")
            columns = [row[1] for row in cursor.fetchall()]
            if "version" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            if "owner_id" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN owner_id TEXT")
            if "lease_expires_at" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN lease_expires_at TEXT")
            if "lease_epoch" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0")
            

    def _migrate_legacy_json(self) -> None:
        legacy_file = self.store_dir / "tasks.json"
        if not legacy_file.exists():
            return
            
        # Check if tasks table is empty, if not, sqlite takes precedence
        with self._connect() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM tasks")
            count = cursor.fetchone()[0]
            if count > 0:
                return
                
        try:
            with legacy_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            
            tasks_to_migrate = {}
            for task_id, raw in data.items():
                if isinstance(raw, dict):
                    tasks_to_migrate[task_id] = Task.from_dict(raw)
                    
            if not tasks_to_migrate:
                return
                
            # Insert transactionally
            with self._connect() as conn:
                for task in tasks_to_migrate.values():
                    self.create_task(task)
                    
            # Safe backup
            legacy_file.rename(legacy_file.with_suffix(".json.bak"))
        except (OSError, json.JSONDecodeError, ValueError) as err:
            logger.error(f"Failed to migrate legacy tasks.json: {err}")

    def create_task(self, task: Task) -> None:
        """Insert a newly created task."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, goal, priority, status, progress, retries, 
                        max_retries, depends_on, created_at, updated_at, 
                        result, error, metadata, checkpoints, history, version, owner_id, lease_expires_at, lease_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.id, task.title, task.goal, task.priority.name,
                        task.status.value, task.progress, task.retries,
                        task.max_retries, json.dumps(task.depends_on),
                        task.created_at, task.updated_at, task.result,
                        task.error, json.dumps(task.metadata),
                        json.dumps([asdict(cp) for cp in task.checkpoints]),
                        json.dumps([asdict(rec) for rec in task.history]),
                        task.version,
                        task.owner_id,
                        task.lease_expires_at,
                        task.lease_epoch,
                    )
                )
        except sqlite3.Error as err:
            logger.error(f"Failed to create task {task.id}: {err}")
            raise

    def update_task(
        self,
        task: Task,
        *,
        expected_status: TaskStatus | None = None,
        expected_owner_id: str | None = None,
        expected_lease_epoch: int | None = None,
        require_unexpired_lease: bool = False,
    ) -> None:
        """Update an existing task safely using OCC."""
        try:
            with self._connect() as conn:
                where = "WHERE id = ? AND version = ?"
                params: list[Any] = [
                    task.title, task.goal, task.priority.name,
                    task.status.value, task.progress, task.retries,
                    task.max_retries, json.dumps(task.depends_on),
                    task.created_at, task.updated_at, task.result,
                    task.error, json.dumps(task.metadata),
                    json.dumps([asdict(cp) for cp in task.checkpoints]),
                    json.dumps([asdict(rec) for rec in task.history]),
                    task.owner_id, task.lease_expires_at, task.lease_epoch,
                    task.id, task.version,
                ]
                if expected_status is not None:
                    where += " AND status = ?"
                    params.append(expected_status.value)
                if expected_owner_id is not None:
                    where += " AND owner_id = ?"
                    params.append(expected_owner_id)
                if expected_lease_epoch is not None:
                    where += " AND lease_epoch = ?"
                    params.append(expected_lease_epoch)
                if require_unexpired_lease:
                    where += " AND lease_expires_at > ?"
                    params.append(datetime.now(UTC).isoformat())
                cursor = conn.execute(
                    """
                    UPDATE tasks SET
                        title=?, goal=?, priority=?, status=?, progress=?,
                        retries=?, max_retries=?, depends_on=?, created_at=?,
                        updated_at=?, result=?, error=?, metadata=?,
                        checkpoints=?, history=?, version=version + 1,
                        owner_id=?, lease_expires_at=?, lease_epoch=?
                    """ + where,
                    params,
                )
                if cursor.rowcount == 0:
                    raise TaskConcurrencyError(task.id)
        except sqlite3.Error as err:
            logger.error(f"Failed to update task {task.id}: {err}")
            raise

    def load(self) -> dict[str, Task]:
        """Load all tasks from SQLite."""
        if not self.store_file.exists():
            return {}
        tasks = {}
        try:
            with self._connect() as conn:
                cursor = conn.execute("SELECT * FROM tasks")
                columns = [col[0] for col in cursor.description]
                for row in cursor.fetchall():
                    row_dict = dict(zip(columns, row))
                    
                    data = {
                        "id": row_dict["id"],
                        "title": row_dict["title"],
                        "goal": row_dict["goal"],
                        "priority": row_dict["priority"],
                        "status": row_dict["status"],
                        "progress": row_dict["progress"],
                        "retries": row_dict["retries"],
                        "max_retries": row_dict["max_retries"],
                        "depends_on": json.loads(row_dict["depends_on"]),
                        "created_at": row_dict["created_at"],
                        "updated_at": row_dict["updated_at"],
                        "result": row_dict["result"],
                        "error": row_dict["error"],
                        "metadata": json.loads(row_dict["metadata"]),
                        "checkpoints": json.loads(row_dict["checkpoints"]),
                        "history": json.loads(row_dict["history"]),
                        "version": row_dict["version"],
                        "owner_id": row_dict.get("owner_id"),
                        "lease_expires_at": row_dict.get("lease_expires_at"),
                        "lease_epoch": row_dict.get("lease_epoch", 0),
                    }
                    tasks[row_dict["id"]] = Task.from_dict(data)
            return tasks
        except (sqlite3.Error, json.JSONDecodeError) as err:
            logger.error(f"Failed to load task store: {err}")
            return {}


EventListener = Callable[[TaskEvent], Awaitable[None] | None]


class TaskEventBus:
    """Async event bus emitting structured task events for VS Code / RPC clients."""

    def __init__(self) -> None:
        self._listeners: list[EventListener] = []

    def subscribe(self, listener: EventListener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener: EventListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def emit(self, event: TaskEvent) -> None:
        for listener in list(self._listeners):
            try:
                res = listener(event)
                if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                    await res
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception as err:  # noqa: BLE001
                # Intentionally broad to isolate event listener failures from crashing the task manager.
                logger.warning(f"Error in task event listener: {err}")


# ---------------------------------------------------------------------------
# Task Manager
# ---------------------------------------------------------------------------


class TaskManager:
    """Principal Task Manager for Pulse.

    Handles task creation, priority queue scheduling, status lifecycle,
    persistence, checkpoints, telemetry logging, long-term memory updates,
    and VS Code event broadcasts.
    """
    CANCELLATION_GRACE_SECONDS = 1.0

    def __init__(
        self,
        workspace: Path | None = None,
        *,
        telemetry: Any | None = None,
        memory: Any | None = None,
        store: TaskStore | None = None,
        event_bus: TaskEventBus | None = None,
        heartbeat_interval: float = 15.0,
        lease_duration: float = 60.0,
    ) -> None:
        if heartbeat_interval <= 0 or lease_duration <= 0 or heartbeat_interval >= lease_duration:
            raise ValueError("Invalid heartbeat config: interval must be > 0 and strictly less than lease_duration.")

        self.workspace = workspace or Path.cwd()
        self.worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        self.heartbeat_interval = heartbeat_interval
        self.lease_duration = lease_duration
        self.telemetry = telemetry
        self.memory = memory
        self.store = store or TaskStore(self.workspace)
        self.event_bus = event_bus or TaskEventBus()
        self._tasks: dict[str, Task] = self.store.load()
        self._queue: list[str] = []
        self._lock = asyncio.Lock()
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}
        # Tracks leases acquired by this manager.  It lets mutation methods
        # distinguish an active worker from legacy/manual lifecycle calls that
        # may transition a queued task directly.
        self._lease_epochs: dict[str, int] = {}
        self._uncontained_tasks: set[asyncio.Task] = set()
        
        # We don't automatically recover in __init__ because we want async startup.
        # But for simplicity, we provide a `recover_startup_tasks` method to be called.

    # ---------------------------------------------------------------------------
    # Public Task Lifecycle Methods
    # ---------------------------------------------------------------------------

    async def recover_tasks(self) -> None:
        """Scan for orphaned RUNNING tasks and recover them if leases expired."""
        tasks_to_recover = []
        async with self._lock:
            for task in self._tasks.values():
                if task.status == TaskStatus.RUNNING:
                    if not task.lease_expires_at:
                        tasks_to_recover.append(task.id)
                    else:
                        try:
                            expires_at = datetime.fromisoformat(task.lease_expires_at)
                            if datetime.now(UTC) > expires_at:
                                tasks_to_recover.append(task.id)
                        except ValueError:
                            tasks_to_recover.append(task.id)

        for task_id in tasks_to_recover:
            await self._recover_single_task(task_id)

    async def _recover_single_task(self, task_id: str) -> None:
        for attempt in range(3):
            async with self._lock:
                task = self._get_task_or_raise(task_id)
                if task.status != TaskStatus.RUNNING:
                    return
                if task.lease_expires_at:
                    try:
                        if datetime.now(UTC) <= datetime.fromisoformat(task.lease_expires_at):
                            return
                    except ValueError:
                        pass
                
                is_remote = task.metadata.get("execution_mode") == "remote"
                
                task.owner_id = None
                task.lease_expires_at = None
                # Recovery invalidates every capability held by the old executor.
                task.lease_epoch += 1
                
                if is_remote:
                    task.status = TaskStatus.RECOVERY_PENDING
                    detail = "Remote execution could not be reconciled. Task moved to RECOVERY_PENDING."
                else:
                    task.status = TaskStatus.QUEUED
                    if task_id not in self._queue:
                        self._queue.append(task_id)
                    self._sort_queue()
                    task.retries += 1
                    detail = "Recovered orphaned task from stale lease."

                task.updated_at = datetime.now(UTC).isoformat()
                task.history.append(
                    TaskExecutionRecord(
                        timestamp=datetime.now(UTC).isoformat(),
                        action="recovery_initiated",
                        detail=detail,
                        success=not is_remote
                    )
                )

                try:
                    self.store.update_task(task)
                    task.version += 1
                    break
                except TaskConcurrencyError:
                    fresh_tasks = self.store.load()
                    if task.id in fresh_tasks:
                        self._tasks[task.id] = fresh_tasks[task.id]
                    if attempt == 2:
                        raise

    async def create_task(
        self,
        goal: str,
        *,
        title: str = "",
        priority: TaskPriority = TaskPriority.MEDIUM,
        depends_on: Sequence[str] = (),
        max_retries: int = 3,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        """Create and store a new task."""
        async with self._lock:
            task_id = f"task-{uuid.uuid4().hex[:8]}"
            clean_title = title.strip() or (goal[:45] + "..." if len(goal) > 45 else goal)

            task = Task(
                id=task_id,
                title=clean_title,
                goal=goal,
                priority=priority,
                status=TaskStatus.PENDING,
                depends_on=list(depends_on),
                max_retries=max_retries,
                metadata=metadata or {},
            )
            self._tasks[task_id] = task
            self.store.create_task(task)

        await self._emit_event("task_created", task)
        self._log_telemetry("task_created", task_id=task_id, priority=priority.name)
        return task

    async def queue_task(self, task_id: str) -> Task:
        """Transition task to QUEUED and place it into priority queue."""
        for attempt in range(3):
            async with self._lock:
                task = self._get_task_or_raise(task_id)
                if task.status in (TaskStatus.RUNNING, TaskStatus.COMPLETED):
                    return task

                task.status = TaskStatus.QUEUED
                task.updated_at = datetime.now(UTC).isoformat()
                if task_id not in self._queue:
                    self._queue.append(task_id)
                self._sort_queue()
                try:
                    self.store.update_task(task)
                    task.version += 1
                    break  # OCC success
                except TaskConcurrencyError:
                    # Reload the authoritative state from DB to heal our cache
                    fresh_tasks = self.store.load()
                    if task.id in fresh_tasks:
                        self._tasks[task.id] = fresh_tasks[task.id]
                    if attempt == 2:
                        raise

        await self._emit_event("task_queued", task)
        return task

    async def start_task(self, task_id: str) -> Task:
        """Transition task to RUNNING status."""
        for attempt in range(3):
            async with self._lock:
                task = self._get_task_or_raise(task_id)

                # Check dependencies
                unresolved = [
                    dep_id for dep_id in task.depends_on
                    if dep_id in self._tasks and self._tasks[dep_id].status != TaskStatus.COMPLETED
                ]
                if unresolved:
                    raise RuntimeError(f"Cannot start task {task_id}; unresolved dependencies: {unresolved}")

                # Guard acquisition
                if task.status not in (TaskStatus.QUEUED, TaskStatus.PENDING):
                    # We can also steal it if it's RUNNING but expired. 
                    if task.status == TaskStatus.RUNNING and task.lease_expires_at:
                        try:
                            expires_at = datetime.fromisoformat(task.lease_expires_at)
                            if datetime.now(UTC) <= expires_at:
                                raise RuntimeError(f"Task {task_id} is currently owned and lease is active.")
                        except ValueError:
                            raise RuntimeError(f"Task {task_id} has invalid lease.")
                    else:
                        raise RuntimeError(f"Cannot start task {task_id}; wrong status {task.status.value}")

                task.status = TaskStatus.RUNNING
                task.owner_id = self.worker_id
                task.lease_epoch += 1
                task.lease_expires_at = (datetime.now(UTC) + timedelta(seconds=self.lease_duration)).isoformat()
                task.updated_at = datetime.now(UTC).isoformat()
                if task_id in self._queue:
                    self._queue.remove(task_id)
                try:
                    self.store.update_task(task)
                    task.version += 1
                    self._lease_epochs[task_id] = task.lease_epoch
                    break  # OCC success
                except TaskConcurrencyError:
                    # Reload the authoritative state from DB to heal our cache
                    fresh_tasks = self.store.load()
                    if task.id in fresh_tasks:
                        self._tasks[task.id] = fresh_tasks[task.id]
                    if attempt == 2:
                        raise

        await self._emit_event("task_started", task)
        self._log_telemetry("task_started", task_id=task_id)
        return task

    async def update_progress(self, task_id: str, progress: float, detail: str = "") -> Task:
        """Update task progress percentage (0.0 - 100.0)."""
        for attempt in range(3):
            async with self._lock:
                task = self._check_ownership(self._get_task_or_raise(task_id))
                fence = self._fence_for(task)
                
                task.progress = min(max(progress, 0.0), 100.0)
                task.updated_at = datetime.now(UTC).isoformat()
                if detail:
                    task.history.append(
                        TaskExecutionRecord(
                            timestamp=datetime.now(UTC).isoformat(),
                            action="progress_update",
                            detail=detail,
                        )
                    )
                try:
                    self._update_task_with_fence(task, fence)
                    task.version += 1
                    break  # OCC success
                except TaskConcurrencyError:
                    # Reload the authoritative state from DB to heal our cache
                    fresh_tasks = self.store.load()
                    if task.id in fresh_tasks:
                        self._tasks[task.id] = fresh_tasks[task.id]
                    if attempt == 2:
                        raise

        await self._emit_event("task_progress", task, {"detail": detail})
        return task

    async def pause_task(self, task_id: str, reason: str = "") -> Task:
        """Pause a running or queued task."""
        for attempt in range(3):
            async with self._lock:
                task = self._check_ownership(self._get_task_or_raise(task_id))
                fence = self._fence_for(task)
                
                task.status = TaskStatus.PAUSED
                task.owner_id = None
                task.lease_expires_at = None
                self._stop_heartbeat(task_id)
                task.updated_at = datetime.now(UTC).isoformat()
                if task_id in self._queue:
                    self._queue.remove(task_id)
                task.history.append(
                    TaskExecutionRecord(
                        timestamp=datetime.now(UTC).isoformat(),
                        action="paused",
                        detail=reason or "Task paused by user",
                    )
                )
                try:
                    self._update_task_with_fence(task, fence)
                    task.version += 1
                    break  # OCC success
                except TaskConcurrencyError:
                    # Reload the authoritative state from DB to heal our cache
                    fresh_tasks = self.store.load()
                    if task.id in fresh_tasks:
                        self._tasks[task.id] = fresh_tasks[task.id]
                    if attempt == 2:
                        raise

        self._lease_epochs.pop(task_id, None)
        await self._emit_event("task_paused", task, {"reason": reason})
        self._log_telemetry("task_paused", task_id=task_id, reason=reason)
        return task

    async def resume_task(self, task_id: str) -> Task:
        """Resume a paused or failed task from its latest checkpoint."""
        for attempt in range(3):
            async with self._lock:
                task = self._get_task_or_raise(task_id)
                if task.status not in (TaskStatus.PAUSED, TaskStatus.FAILED):
                    raise ValueError(f"Task {task_id} is in state {task.status.value} and cannot be resumed.")

                task.status = TaskStatus.QUEUED
                task.updated_at = datetime.now(UTC).isoformat()
                if task_id not in self._queue:
                    self._queue.append(task_id)
                self._sort_queue()

                latest_cp = task.checkpoints[-1] if task.checkpoints else None
                detail = f"Resumed from checkpoint {latest_cp.checkpoint_id}" if latest_cp else "Resumed execution"

                task.history.append(
                    TaskExecutionRecord(
                        timestamp=datetime.now(UTC).isoformat(),
                        action="resumed",
                        detail=detail,
                    )
                )
                try:
                    self.store.update_task(task)
                    task.version += 1
                    self._lease_epochs.pop(task_id, None)
                    break  # OCC success
                except TaskConcurrencyError:
                    # Reload the authoritative state from DB to heal our cache
                    fresh_tasks = self.store.load()
                    if task.id in fresh_tasks:
                        self._tasks[task.id] = fresh_tasks[task.id]
                    if attempt == 2:
                        raise

        await self._emit_event("task_resumed", task)
        self._log_telemetry("task_resumed", task_id=task_id)
        return task

    async def cancel_task(self, task_id: str, reason: str = "") -> Task:
        """Cancel a pending, queued, or running task."""
        for attempt in range(3):
            async with self._lock:
                task = self._check_ownership(self._get_task_or_raise(task_id))
                fence = self._fence_for(task)
                
                task.status = TaskStatus.CANCELLED
                task.owner_id = None
                task.lease_expires_at = None
                self._stop_heartbeat(task_id)
                task.updated_at = datetime.now(UTC).isoformat()
                if task_id in self._queue:
                    self._queue.remove(task_id)
                task.history.append(
                    TaskExecutionRecord(
                        timestamp=datetime.now(UTC).isoformat(),
                        action="cancelled",
                        detail=reason or "Task cancelled by user",
                    )
                )
                try:
                    self._update_task_with_fence(task, fence)
                    task.version += 1
                    self._lease_epochs.pop(task_id, None)
                    break  # OCC success
                except TaskConcurrencyError:
                    # Reload the authoritative state from DB to heal our cache
                    fresh_tasks = self.store.load()
                    if task.id in fresh_tasks:
                        self._tasks[task.id] = fresh_tasks[task.id]
                    if attempt == 2:
                        raise

        await self._emit_event("task_cancelled", task, {"reason": reason})
        self._log_telemetry("task_cancelled", task_id=task_id, reason=reason)
        return task

    async def complete_task(self, task_id: str, result: str = "") -> Task:
        """Mark task as successfully COMPLETED."""
        for attempt in range(3):
            async with self._lock:
                task = self._check_ownership(self._get_task_or_raise(task_id))
                fence = self._fence_for(task)
                
                task.status = TaskStatus.COMPLETED
                task.owner_id = None
                task.lease_expires_at = None
                self._stop_heartbeat(task_id)
                task.progress = 100.0
                task.result = result
                task.updated_at = datetime.now(UTC).isoformat()
                task.history.append(
                    TaskExecutionRecord(
                        timestamp=datetime.now(UTC).isoformat(),
                        action="completed",
                        detail=f"Task completed: {result[:60]}",
                    )
                )
                try:
                    self._update_task_with_fence(task, fence)
                    task.version += 1
                    self._lease_epochs.pop(task_id, None)
                    break  # OCC success
                except TaskConcurrencyError:
                    # Reload the authoritative state from DB to heal our cache
                    fresh_tasks = self.store.load()
                    if task.id in fresh_tasks:
                        self._tasks[task.id] = fresh_tasks[task.id]
                    if attempt == 2:
                        raise

            # Update long-term memory if available
            if self.memory and hasattr(self.memory, "save_context"):
                try:
                    await self.memory.save_context(f"Completed task '{task.title}': {result[:200]}")
                # Intentionally broad to isolate execution boundaries and prevent crashes.
                except Exception as err:  # noqa: BLE001
                    # Intentionally broad to prevent memory update failures from crashing the task loop.
                    logger.warning(f"Memory update failed: {err}")

        await self._emit_event("task_completed", task, {"result": result})
        self._log_telemetry("task_completed", task_id=task_id)
        return task

    async def fail_task(self, task_id: str, error: str) -> Task:
        """Mark task as FAILED or trigger retry if max_retries not reached."""
        for attempt in range(3):
            async with self._lock:
                task = self._check_ownership(self._get_task_or_raise(task_id))
                fence = self._fence_for(task)
                
                task.owner_id = None
                task.lease_expires_at = None
                self._stop_heartbeat(task_id)
                
                task.retries += 1
                task.updated_at = datetime.now(UTC).isoformat()
                task.error = error

                if task.retries <= task.max_retries:
                    task.status = TaskStatus.QUEUED
                    if task_id not in self._queue:
                        self._queue.append(task_id)
                    self._sort_queue()
                    task.history.append(
                        TaskExecutionRecord(
                            timestamp=datetime.now(UTC).isoformat(),
                            action="retry_scheduled",
                            detail=f"Retry {task.retries}/{task.max_retries} after error: {error[:60]}",
                            success=False,
                        )
                    )
                    event_name = "task_retry_scheduled"
                else:
                    task.status = TaskStatus.FAILED
                    task.history.append(
                        TaskExecutionRecord(
                            timestamp=datetime.now(UTC).isoformat(),
                            action="failed",
                            detail=f"Task failed permanently: {error[:60]}",
                            success=False,
                        )
                    )
                    event_name = "task_failed"

                try:
                    self._update_task_with_fence(task, fence)
                    task.version += 1
                    break  # OCC success
                except TaskConcurrencyError:
                    # Reload the authoritative state from DB to heal our cache
                    fresh_tasks = self.store.load()
                    if task.id in fresh_tasks:
                        self._tasks[task.id] = fresh_tasks[task.id]
                    if attempt == 2:
                        raise

        self._lease_epochs.pop(task_id, None)
        await self._emit_event(event_name, task, {"error": error})
        self._log_telemetry(event_name, task_id=task_id, error=error)
        return task

    # ---------------------------------------------------------------------------
    # Checkpointing Methods
    # ---------------------------------------------------------------------------

    async def create_checkpoint(
        self, task_id: str, step_index: int, state_data: dict[str, Any]
    ) -> TaskCheckpoint:
        """Create and append a state checkpoint for a task."""
        for attempt in range(3):
            async with self._lock:
                task = self._check_ownership(self._get_task_or_raise(task_id))
                fence = self._fence_for(task)
                
                cp_id = f"cp-{task_id}-{step_index}-{uuid.uuid4().hex[:4]}"
                checkpoint = TaskCheckpoint(
                    checkpoint_id=cp_id,
                    task_id=task_id,
                    step_index=step_index,
                    state_data=state_data,
                )
                task.checkpoints.append(checkpoint)
                task.updated_at = datetime.now(UTC).isoformat()
                try:
                    self._update_task_with_fence(task, fence)
                    task.version += 1
                    break  # OCC success
                except TaskConcurrencyError:
                    # Reload the authoritative state from DB to heal our cache
                    fresh_tasks = self.store.load()
                    if task.id in fresh_tasks:
                        self._tasks[task.id] = fresh_tasks[task.id]
                    if attempt == 2:
                        raise

        await self._emit_event("task_checkpoint_saved", task, {"checkpoint_id": cp_id})
        return checkpoint

    async def restore_checkpoint(self, task_id: str, checkpoint_id: str | None = None) -> TaskCheckpoint:
        """Retrieve a checkpoint for restoration."""
        for attempt in range(3):
            async with self._lock:
                task = self._get_task_or_raise(task_id)
                if not task.checkpoints:
                    raise ValueError(f"Task {task_id} has no checkpoints.")

                if checkpoint_id:
                    cp = next((c for c in task.checkpoints if c.checkpoint_id == checkpoint_id), None)
                    if not cp:
                        raise ValueError(f"Checkpoint {checkpoint_id} not found for task {task_id}.")
                    return cp

                return task.checkpoints[-1]  # latest checkpoint
    # ---------------------------------------------------------------------------
    # Query & Worker Queue Methods
    # ---------------------------------------------------------------------------

    def get_task(self, task_id: str) -> Task | None:
        """Retrieve task by ID."""
        return self._tasks.get(task_id)
    def list_tasks(self, *, status: TaskStatus | None = None) -> list[Task]:
        """List all tasks, optionally filtered by status, sorted by priority and created_at."""
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return sorted(tasks, key=lambda t: (-t.priority.value, t.created_at))

    async def process_queue(
        self, worker_func: Callable[[Task], Awaitable[str]]
    ) -> list[Task]:
        """Processes queued tasks with lease-bound execution supervision.

        Each worker_func invocation is wrapped in an asyncio.Task that is
        raced against the heartbeat task via asyncio.wait(FIRST_COMPLETED).
        If the heartbeat detects ownership loss (StaleWorkerError), it
        raises LeaseLostError, which causes the supervisor to immediately
        cancel worker_func.  This prevents the duplicate-execution
        vulnerability where a stale worker continues running after its
        lease has been recovered by another process.
        """
        processed: list[Task] = []

        async with self._lock:
            ready_ids = list(self._queue)

        processed: list[Task] = []
        for task_id in ready_ids:
            result = await self.execute_task(task_id, worker_func)
            if result is not None:
                processed.append(result)
        return processed

        for task_id in ready_ids:
            try:
                task = await self.start_task(task_id)
            # Intentionally broad to isolate start failures.
            except Exception as err:  # noqa: BLE001
                logger.warning(f"Failed to start task {task_id}: {err}")
                try:
                    failed = await self.fail_task(task_id, str(err))
                    processed.append(failed)
                except (StaleWorkerError, LeaseLostError):
                    pass
                continue

            # Create supervised execution: worker + heartbeat run concurrently.
            # The heartbeat was already started by start_task().
            worker_task = asyncio.create_task(worker_func(task))
            heartbeat_task = self._heartbeat_tasks.get(task_id)

            try:
                result_str = await self._supervise_execution(
                    task_id, worker_task, heartbeat_task
                )
                completed = await self.complete_task(task_id, result_str)
                processed.append(completed)
            except LeaseLostError:
                # Ownership lost — cancel worker, do NOT complete/fail.
                # The recovered/new owner will handle this task.
                logger.warning(
                    f"Lease lost for task {task_id}; worker cancelled. "
                    f"Recovery by new owner expected."
                )
            except asyncio.CancelledError:
                # process_queue itself was cancelled — clean up both tasks.
                raise
            except StaleWorkerError:
                # complete_task raised StaleWorkerError — ownership lost
                # between worker completion and finalization.  Do nothing;
                # the new owner will handle the task.
                logger.warning(
                    f"Stale worker for task {task_id} at completion; "
                    f"discarding result."
                )
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception as err:  # noqa: BLE001
                # Worker raised an exception — normal failure path.
                try:
                    failed = await self.fail_task(task_id, str(err))
                    processed.append(failed)
                except (StaleWorkerError, LeaseLostError):
                    logger.warning(
                        f"Cannot fail task {task_id}: ownership lost."
                    )
            finally:
                # Every execution path observes both children before moving on.
                await self._cancel_and_await(worker_task)
                await self._cancel_and_await(heartbeat_task)
                if self._heartbeat_tasks.get(task_id) is heartbeat_task:
                    del self._heartbeat_tasks[task_id]

        return processed

    async def execute_task(
        self, task_id: str, worker_func: Callable[[Task], Awaitable[str]]
    ) -> Task | None:
        """Run one local worker under the authoritative lease supervisor."""
        try:
            task = await self.start_task(task_id)
        except Exception as err:  # noqa: BLE001
            try:
                return await self.fail_task(task_id, str(err))
            except (StaleWorkerError, LeaseLostError):
                logger.warning("Cannot record start failure for %s.", task_id)
                return None
        worker_task = asyncio.create_task(worker_func(task))
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(task_id))
        self._heartbeat_tasks[task_id] = heartbeat_task
        try:
            result = await self._supervise_execution(task_id, worker_task, heartbeat_task)
            return await self.complete_task(task_id, result)
        except LeaseLostError:
            logger.warning("Lease lost for task %s; execution fenced.", task_id)
            return None
        except asyncio.CancelledError:
            raise
        except StaleWorkerError:
            logger.warning("Stale worker for task %s; discarding result.", task_id)
            return None
        except Exception as err:  # noqa: BLE001
            try:
                return await self.fail_task(task_id, str(err))
            except StaleWorkerError:
                logger.warning("Cannot fail task %s: ownership lost.", task_id)
                return None
        finally:
            await self._cancel_and_await(worker_task)
            await self._cancel_and_await(heartbeat_task)
            if self._heartbeat_tasks.get(task_id) is heartbeat_task:
                del self._heartbeat_tasks[task_id]

    async def _supervise_execution(
        self,
        task_id: str,
        worker_task: asyncio.Task,
        heartbeat_task: asyncio.Task | None,
    ) -> str:
        """Supervise worker execution, cancelling it if the lease is lost.

        Races worker_task against heartbeat_task using
        asyncio.wait(FIRST_COMPLETED).

        Returns:
            The worker result string on success.

        Raises:
            LeaseLostError: if the heartbeat detects ownership loss.
            Exception: re-raises the worker's exception if the worker fails.
        """
        if heartbeat_task is None:
            raise LeaseLostError(task_id)

        done, _pending = await asyncio.wait(
            {worker_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
        )
        # A simultaneous completion is a lease-loss outcome: blindly choosing
        # the worker would permit its result to win an ownership-loss race.
        if heartbeat_task in done:
            await self._cancel_and_await(worker_task)
            if heartbeat_task.cancelled():
                raise LeaseLostError(task_id)
            exc = heartbeat_task.exception()
            if exc is not None:
                raise exc
            raise LeaseLostError(task_id)

        # The worker finished first, but the database remains authoritative.
        # Check ownership before stopping the heartbeat and before completion.
        self._verify_active_ownership(task_id)
        await self._cancel_and_await(heartbeat_task)
        return worker_task.result()

    # ---------------------------------------------------------------------------
    # Internal Helpers
    # ---------------------------------------------------------------------------

    def _check_ownership(self, task: Task) -> Task:
        """Return the authoritative record and enforce its durable lease fence."""
        fresh = self.store.load().get(task.id)
        if fresh is None:
            raise StaleWorkerError(task.id)
        self._tasks[task.id] = fresh
        local_epoch = self._lease_epochs.get(task.id)
        lease_valid = False
        if fresh.lease_expires_at:
            try:
                lease_valid = datetime.now(UTC) < datetime.fromisoformat(fresh.lease_expires_at)
            except ValueError:
                lease_valid = False
        if (fresh.status == TaskStatus.RUNNING or local_epoch is not None) and (
            fresh.status != TaskStatus.RUNNING
            or fresh.owner_id != self.worker_id
            or local_epoch != fresh.lease_epoch
            or not lease_valid
        ):
            raise StaleWorkerError(task.id)
        return fresh

    def _fence_for(self, task: Task) -> tuple[str, int] | None:
        """Capture the durable execution capability before a mutation."""
        if task.status != TaskStatus.RUNNING:
            return None
        return (self.worker_id, task.lease_epoch)

    def _update_task_with_fence(
        self, task: Task, fence: tuple[str, int] | None
    ) -> None:
        if fence is None:
            self.store.update_task(task)
            return
        owner_id, epoch = fence
        self.store.update_task(
            task,
            expected_status=TaskStatus.RUNNING,
            expected_owner_id=owner_id,
            expected_lease_epoch=epoch,
            require_unexpired_lease=True,
        )

    def _verify_active_ownership(self, task_id: str) -> None:
        """Load the authoritative record before finalizing worker output."""
        task = self.store.load().get(task_id)
        if (
            task is None
            or task.status != TaskStatus.RUNNING
            or task.owner_id != self.worker_id
        ):
            raise LeaseLostError(task_id)
        self._tasks[task_id] = task

    async def _cancel_and_await(self, task: asyncio.Task | None) -> None:
        """Cancel a child with a bounded wait; never treat timeout as stopped."""
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self.CANCELLATION_GRACE_SECONDS
            )
        except asyncio.CancelledError:
            pass
        except TimeoutError as err:
            # Keep a strong reference so an uncontained task is visible rather
            # than becoming an unobserved background task.  Its lease epoch is
            # already fenced, so it cannot commit TaskManager mutations.
            self._uncontained_tasks.add(task)
            task.add_done_callback(self._uncontained_tasks.discard)
            logger.critical("Worker ignored cancellation; lease remains fenced.")
            raise LeaseLostError("uncontained-worker") from err

    def _stop_heartbeat(self, task_id: str) -> None:
        if task_id in self._heartbeat_tasks:
            self._heartbeat_tasks[task_id].cancel()
            del self._heartbeat_tasks[task_id]

    async def _heartbeat_loop(self, task_id: str) -> None:
        """Background heartbeat that renews the lease periodically.

        If ownership is lost (StaleWorkerError from renew_lease), this
        method raises LeaseLostError so that the execution supervisor
        (_supervise_execution) can observe it and cancel the worker.

        CancelledError is swallowed — it indicates normal shutdown by
        a terminal transition (complete_task, fail_task, etc.).
        """
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                await self.renew_lease(task_id)
        except asyncio.CancelledError:
            pass  # Normal cancellation by completion/failure/pause
        except StaleWorkerError:
            # Ownership conclusively lost — signal supervisor.
            raise LeaseLostError(task_id)
        except Exception as e:  # noqa: BLE001
            # Any other failure (DB error, etc.) — treat as lease lost
            # because we can no longer guarantee ownership.
            logger.warning(f"Heartbeat loop for {task_id} failed: {e}")
            raise LeaseLostError(task_id)

    async def renew_lease(self, task_id: str) -> None:
        for attempt in range(3):
            async with self._lock:
                task = self._check_ownership(self._get_task_or_raise(task_id))
                fence = self._fence_for(task)

                task.lease_expires_at = (datetime.now(UTC) + timedelta(seconds=self.lease_duration)).isoformat()
                
                try:
                    self._update_task_with_fence(task, fence)
                    task.version += 1
                    break
                except TaskConcurrencyError:
                    fresh_tasks = self.store.load()
                    if task.id in fresh_tasks:
                        self._tasks[task.id] = fresh_tasks[task.id]
                    if attempt == 2:
                        raise

    def _get_task_or_raise(self, task_id: str) -> Task:
        if task_id not in self._tasks:
            raise KeyError(f"Task with ID '{task_id}' not found.")
        return self._tasks[task_id]

    def _sort_queue(self) -> None:
        """Sort queue by priority (descending) and created_at (ascending)."""
        self._queue.sort(
            key=lambda tid: (
                -self._tasks[tid].priority.value if tid in self._tasks else 0,
                self._tasks[tid].created_at if tid in self._tasks else "",
            )
        )


    async def _emit_event(
        self, event_type: str, task: Task, payload: dict[str, Any] | None = None
    ) -> None:
        event = TaskEvent(
            event_type=event_type,
            task_id=task.id,
            status=task.status,
            progress=task.progress,
            payload={**(payload or {}), "title": task.title},
        )
        await self.event_bus.emit(event)

    def _log_telemetry(self, event_type: str, **kwargs: Any) -> None:
        if self.telemetry and hasattr(self.telemetry, "log_event"):
            try:
                self.telemetry.log_event(event_type=f"task_manager_{event_type}", **kwargs)
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception as err:  # noqa: BLE001
                # Intentionally broad to prevent telemetry failures from crashing the system.
                logger.warning(f"Telemetry logging failed: {err}")
