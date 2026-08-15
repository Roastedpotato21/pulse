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
from datetime import UTC, datetime
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


class TaskStatus(Enum):
    """Current state of a managed task."""

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


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
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                """
            )
            # Safe schema migration for OCC
            cursor = conn.execute("PRAGMA table_info(tasks)")
            columns = [row[1] for row in cursor.fetchall()]
            if "version" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            

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
                        result, error, metadata, checkpoints, history, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    )
                )
        except sqlite3.Error as err:
            logger.error(f"Failed to create task {task.id}: {err}")
            raise

    def update_task(self, task: Task) -> None:
        """Update an existing task safely using OCC."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE tasks SET
                        title=?, goal=?, priority=?, status=?, progress=?,
                        retries=?, max_retries=?, depends_on=?, created_at=?,
                        updated_at=?, result=?, error=?, metadata=?,
                        checkpoints=?, history=?, version=version + 1
                    WHERE id = ? AND version = ?
                    """,
                    (
                        task.title, task.goal, task.priority.name,
                        task.status.value, task.progress, task.retries,
                        task.max_retries, json.dumps(task.depends_on),
                        task.created_at, task.updated_at, task.result,
                        task.error, json.dumps(task.metadata),
                        json.dumps([asdict(cp) for cp in task.checkpoints]),
                        json.dumps([asdict(rec) for rec in task.history]),
                        task.id, task.version,
                    )
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

    def __init__(
        self,
        workspace: Path | None = None,
        *,
        telemetry: Any | None = None,
        memory: Any | None = None,
        store: TaskStore | None = None,
        event_bus: TaskEventBus | None = None,
    ) -> None:
        self.workspace = workspace or Path.cwd()
        self.telemetry = telemetry
        self.memory = memory
        self.store = store or TaskStore(self.workspace)
        self.event_bus = event_bus or TaskEventBus()
        self._tasks: dict[str, Task] = self.store.load()
        self._queue: list[str] = []
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------------------
    # Public Task Lifecycle Methods
    # ---------------------------------------------------------------------------

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
            except TaskConcurrencyError:
                # Reload the authoritative state from DB to heal our cache
                fresh_tasks = self.store.load()
                if task.id in fresh_tasks:
                    self._tasks[task.id] = fresh_tasks[task.id]
                raise

        await self._emit_event("task_queued", task)
        return task

    async def start_task(self, task_id: str) -> Task:
        """Transition task to RUNNING status."""
        async with self._lock:
            task = self._get_task_or_raise(task_id)

            # Check dependencies
            unresolved = [
                dep_id for dep_id in task.depends_on
                if dep_id in self._tasks and self._tasks[dep_id].status != TaskStatus.COMPLETED
            ]
            if unresolved:
                raise RuntimeError(f"Cannot start task {task_id}; unresolved dependencies: {unresolved}")

            task.status = TaskStatus.RUNNING
            task.updated_at = datetime.now(UTC).isoformat()
            if task_id in self._queue:
                self._queue.remove(task_id)
            try:
                self.store.update_task(task)
                task.version += 1
            except TaskConcurrencyError:
                # Reload the authoritative state from DB to heal our cache
                fresh_tasks = self.store.load()
                if task.id in fresh_tasks:
                    self._tasks[task.id] = fresh_tasks[task.id]
                raise

        await self._emit_event("task_started", task)
        self._log_telemetry("task_started", task_id=task_id)
        return task

    async def update_progress(self, task_id: str, progress: float, detail: str = "") -> Task:
        """Update task progress percentage (0.0 - 100.0)."""
        async with self._lock:
            task = self._get_task_or_raise(task_id)
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
                self.store.update_task(task)
                task.version += 1
            except TaskConcurrencyError:
                # Reload the authoritative state from DB to heal our cache
                fresh_tasks = self.store.load()
                if task.id in fresh_tasks:
                    self._tasks[task.id] = fresh_tasks[task.id]
                raise

        await self._emit_event("task_progress", task, {"detail": detail})
        return task

    async def pause_task(self, task_id: str, reason: str = "") -> Task:
        """Pause a running or queued task."""
        async with self._lock:
            task = self._get_task_or_raise(task_id)
            task.status = TaskStatus.PAUSED
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
                self.store.update_task(task)
                task.version += 1
            except TaskConcurrencyError:
                # Reload the authoritative state from DB to heal our cache
                fresh_tasks = self.store.load()
                if task.id in fresh_tasks:
                    self._tasks[task.id] = fresh_tasks[task.id]
                raise

        await self._emit_event("task_paused", task, {"reason": reason})
        self._log_telemetry("task_paused", task_id=task_id, reason=reason)
        return task

    async def resume_task(self, task_id: str) -> Task:
        """Resume a paused or failed task from its latest checkpoint."""
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
            except TaskConcurrencyError:
                # Reload the authoritative state from DB to heal our cache
                fresh_tasks = self.store.load()
                if task.id in fresh_tasks:
                    self._tasks[task.id] = fresh_tasks[task.id]
                raise

        await self._emit_event("task_resumed", task)
        self._log_telemetry("task_resumed", task_id=task_id)
        return task

    async def cancel_task(self, task_id: str, reason: str = "") -> Task:
        """Cancel a pending, queued, or running task."""
        async with self._lock:
            task = self._get_task_or_raise(task_id)
            task.status = TaskStatus.CANCELLED
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
                self.store.update_task(task)
                task.version += 1
            except TaskConcurrencyError:
                # Reload the authoritative state from DB to heal our cache
                fresh_tasks = self.store.load()
                if task.id in fresh_tasks:
                    self._tasks[task.id] = fresh_tasks[task.id]
                raise

        await self._emit_event("task_cancelled", task, {"reason": reason})
        self._log_telemetry("task_cancelled", task_id=task_id, reason=reason)
        return task

    async def complete_task(self, task_id: str, result: str = "") -> Task:
        """Mark task as successfully COMPLETED."""
        async with self._lock:
            task = self._get_task_or_raise(task_id)
            task.status = TaskStatus.COMPLETED
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
                self.store.update_task(task)
                task.version += 1
            except TaskConcurrencyError:
                # Reload the authoritative state from DB to heal our cache
                fresh_tasks = self.store.load()
                if task.id in fresh_tasks:
                    self._tasks[task.id] = fresh_tasks[task.id]
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
        async with self._lock:
            task = self._get_task_or_raise(task_id)
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
                self.store.update_task(task)
                task.version += 1
            except TaskConcurrencyError:
                # Reload the authoritative state from DB to heal our cache
                fresh_tasks = self.store.load()
                if task.id in fresh_tasks:
                    self._tasks[task.id] = fresh_tasks[task.id]
                raise

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
        async with self._lock:
            task = self._get_task_or_raise(task_id)
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
                self.store.update_task(task)
                task.version += 1
            except TaskConcurrencyError:
                # Reload the authoritative state from DB to heal our cache
                fresh_tasks = self.store.load()
                if task.id in fresh_tasks:
                    self._tasks[task.id] = fresh_tasks[task.id]
                raise

        await self._emit_event("task_checkpoint_saved", task, {"checkpoint_id": cp_id})
        return checkpoint

    async def restore_checkpoint(self, task_id: str, checkpoint_id: str | None = None) -> TaskCheckpoint:
        """Retrieve a checkpoint for restoration."""
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
        """Processes queued tasks concurrently using priority order."""
        processed: list[Task] = []

        async with self._lock:
            ready_ids = list(self._queue)

        for task_id in ready_ids:
            try:
                task = await self.start_task(task_id)
                result_str = await worker_func(task)
                completed = await self.complete_task(task_id, result_str)
                processed.append(completed)
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception as err:  # noqa: BLE001
                # Intentionally broad to isolate completion handlers and fallback to fail_task.
                failed = await self.fail_task(task_id, str(err))
                processed.append(failed)

        return processed

    # ---------------------------------------------------------------------------
    # Internal Helpers
    # ---------------------------------------------------------------------------

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
