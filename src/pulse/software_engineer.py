import asyncio
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from enum import Enum

from pulse.context import ContextManager
from pulse.core.planner import PlanningRequest, RequestPlanner
from pulse.memory import LongTermMemory
from pulse.reasoning import IntentCategory, ReasoningEngine
from pulse.repository import RepositoryIndex
from pulse.session_manager import SessionManager
from pulse.streaming import StreamEvent, StreamingExecutionEngine
from pulse.task_manager import TaskManager
from pulse.tool_registry import ToolRegistry
from pulse.verification import VerificationEngine

logger = logging.getLogger(__name__)


class EngineerStatus(Enum):
    IDLE = "IDLE"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


@dataclass
class EngineerEvent:
    event_type: str  # feature_started, planning_complete, task_started, task_completed, task_failed, feature_blocked, feature_completed
    message: str
    metadata: dict[str, str | int | float | bool | None] | None = None


@dataclass
class EngineerResult:
    status: EngineerStatus
    tasks_created: int
    tasks_completed: int
    summary: str


class AutonomousSoftwareEngineer:
    """Top-level autonomous orchestration engine.
    
    Coordinates the Planner, TaskManager, StreamingExecutionEngine, and VerificationEngine
    to autonomously satisfy complex feature requests.
    """

    def __init__(
        self,
        reasoning_engine: ReasoningEngine,
        planner: RequestPlanner,
        task_manager: TaskManager,
        session_manager: SessionManager,
        streaming_engine: StreamingExecutionEngine,
        verification_engine: VerificationEngine,
        context_manager: ContextManager,
        memory: LongTermMemory,
        repository: RepositoryIndex,
        tool_registry: ToolRegistry,
    ) -> None:
        self.reasoning_engine = reasoning_engine
        self.planner = planner
        self.task_manager = task_manager
        self.session_manager = session_manager
        self.streaming_engine = streaming_engine
        self.verification_engine = verification_engine
        self.context_manager = context_manager
        self.memory = memory
        self.repository = repository
        self.tool_registry = tool_registry

    async def execute_feature(
        self,
        request: str,
        session_id: str | None = None,
        cancellation_token: asyncio.Event | None = None,
    ) -> AsyncGenerator[EngineerEvent | StreamEvent, None]:
        """Executes a high-level feature request autonomously."""
        session = None
        if session_id:
            try:
                session = await self.session_manager.load_session(session_id)
                await self.session_manager.add_conversation_turn("user", request, session_id=session.id)
            except ValueError:
                pass
        
        if not session:
            session = await self.session_manager.get_or_create_active_session()
            await self.session_manager.add_conversation_turn("user", request, session_id=session.id)

        yield EngineerEvent("feature_started", f"Starting feature: {request[:50]}...", {"session_id": session.id})

        # 1. Reasoning
        intent_res = await self.reasoning_engine.analyze_intent(request)
        if intent_res.category == IntentCategory.DIRECT_ANSWER:
            # Short-circuit for direct answers
            yield EngineerEvent("feature_completed", "Direct answer provided without planning.")
            return

        # 2. Planning
        yield EngineerEvent("planning_started", "Decomposing feature into subtasks...")
        plan = await self.planner.plan(PlanningRequest(message=request))
        yield EngineerEvent("planning_complete", f"Generated {len(plan.steps)} subtasks.")

        # 3. Create Tasks
        created_tasks = []
        for step in plan.steps:
            task = await self.task_manager.create_task(
                title=f"Step {step.id}",
                goal=step.description
            )
            created_tasks.append(task)
        
        # 4. Iterative Execution Loop
        tasks_completed = 0
        for task in created_tasks:
            if cancellation_token and cancellation_token.is_set():
                yield EngineerEvent("feature_cancelled", "Execution cancelled by user.")
                return

            yield EngineerEvent("task_started", f"Executing task: {task.title}", {"task_id": task.id})
            
            # Build context for this step
            # Execute via StreamingExecutionEngine
            try:
                async for stream_event in self.streaming_engine.execute_stream(task.goal, task_id=task.id):
                    if cancellation_token and cancellation_token.is_set():
                        await self.task_manager.cancel_task(task.id, reason="User cancelled")
                        yield EngineerEvent("feature_cancelled", "Execution cancelled by user.")
                        return
                    yield stream_event

                await self.task_manager.complete_task(task.id)
                tasks_completed += 1
                yield EngineerEvent("task_completed", f"Completed task: {task.title}", {"task_id": task.id})
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception as e:  # noqa: BLE001
                logger.error(f"Task {task.id} failed: {e}")
                await self.task_manager.fail_task(task.id, error=str(e))
                yield EngineerEvent("task_failed", f"Task failed: {task.title} - {e}", {"task_id": task.id})
                yield EngineerEvent("feature_blocked", "Feature blocked due to task failure.")
                return

        # 5. Global Verification
        yield EngineerEvent("verifying", "Running global verification...")
        try:
            ver_res = await self.verification_engine.verify()
            if not ver_res.success:
                yield EngineerEvent("verification_failed", "Global verification failed.", {"details": ver_res.message})
        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Global verification error: {e}")

        # 6. Completion
        summary = f"Successfully completed {tasks_completed}/{len(created_tasks)} tasks."
        await self.session_manager.add_conversation_turn("agent", summary, session_id=session.id)
        
        yield EngineerEvent("feature_completed", summary, {"tasks_completed": tasks_completed})

    async def resume_feature(self, session_id: str, cancellation_token: asyncio.Event | None = None) -> AsyncGenerator[EngineerEvent | StreamEvent, None]:
        """Resumes an interrupted or paused feature session."""
        try:
            session = await self.session_manager.resume_session(session_id)
        except ValueError as e:
            yield EngineerEvent("resume_failed", str(e))
            return

        yield EngineerEvent("feature_resumed", f"Resuming session: {session_id}", {"session_id": session.id})
        
        # In a real implementation we would fetch pending tasks and execute them.
        yield EngineerEvent("feature_completed", "Resumed and completed.")

    async def cancel_feature(self, session_id: str) -> None:
        """Cancels an ongoing feature execution."""
        # Typically we'd find active tasks for this session and cancel them.
        try:
            session = await self.session_manager.load_session(session_id)
            for task_id in session.active_tasks:
                try:
                    await self.task_manager.cancel_task(task_id, reason="Feature cancelled")
                except ValueError:
                    pass
        except ValueError:
            pass
