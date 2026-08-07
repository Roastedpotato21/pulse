import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pulse.core.planner import ExecutionPlan, PlanAction, PlanStep
from pulse.reasoning import IntentAnalysis, IntentCategory
from pulse.software_engineer import AutonomousSoftwareEngineer, EngineerEvent
from pulse.streaming import StreamEvent, StreamEventType
from pulse.task_manager import Task, TaskStatus
from pulse.verification import VerificationResult


@pytest.fixture
def reasoning_engine():
    engine = MagicMock()
    engine.analyze_intent = AsyncMock(return_value=IntentAnalysis(
        category=IntentCategory.FILE_EDIT,
        summary="Implement feature",
        confidence_score=0.9
    ))
    return engine


@pytest.fixture
def planner():
    planner = MagicMock()
    planner.plan = AsyncMock(return_value=ExecutionPlan(
        goal="Build feature X",
        steps=(
            PlanStep(id="1", action=PlanAction.LLM, description="Implement X", depends_on=()),
            PlanStep(id="2", action=PlanAction.LLM, description="Test X", depends_on=("1",))
        )
    ))
    return planner


@pytest.fixture
def task_manager():
    tm = MagicMock()
    t1 = Task(id="task1", title="Implement X", goal="Implement X", priority=TaskStatus.PENDING)
    t2 = Task(id="task2", title="Test X", goal="Test X", priority=TaskStatus.PENDING)
    tm.create_task = AsyncMock(side_effect=[t1, t2])
    tm.complete_task = AsyncMock()
    tm.fail_task = AsyncMock()
    tm.cancel_task = AsyncMock()
    return tm


@pytest.fixture
def session_manager():
    sm = MagicMock()
    session = MagicMock()
    session.id = "session1"
    sm.get_or_create_active_session = AsyncMock(return_value=session)
    sm.load_session = AsyncMock(return_value=session)
    sm.add_conversation_turn = AsyncMock()
    return sm


@pytest.fixture
def streaming_engine():
    engine = MagicMock()
    async def mock_execute_stream(request, task_id=None):
        yield StreamEvent(event_type=StreamEventType.TOOL_COMPLETE, content="done")
    engine.execute_stream = mock_execute_stream
    return engine


@pytest.fixture
def verification_engine():
    ve = MagicMock()
    ve.verify = AsyncMock(return_value=VerificationResult(success=True, framework="test", stdout="All good"))
    return ve


@pytest.fixture
def software_engineer(
    reasoning_engine,
    planner,
    task_manager,
    session_manager,
    streaming_engine,
    verification_engine
):
    return AutonomousSoftwareEngineer(
        reasoning_engine=reasoning_engine,
        planner=planner,
        task_manager=task_manager,
        session_manager=session_manager,
        streaming_engine=streaming_engine,
        verification_engine=verification_engine,
        context_manager=MagicMock(),
        memory=MagicMock(),
        repository=MagicMock(),
        tool_registry=MagicMock(),
    )


def test_execute_feature_happy_path(software_engineer):
    events = []
    async def run_test():
        async for event in software_engineer.execute_feature("Build feature X"):
            if isinstance(event, EngineerEvent):
                events.append(event.event_type)
    asyncio.run(run_test())

    assert "feature_started" in events
    assert "planning_complete" in events
    assert "task_started" in events
    assert "task_completed" in events
    assert "verifying" in events
    assert "feature_completed" in events

    assert software_engineer.task_manager.create_task.call_count == 2
    assert software_engineer.task_manager.complete_task.call_count == 2


def test_execute_feature_cancellation(software_engineer):
    cancellation_token = asyncio.Event()
    
    # We will cancel after the first task starts
    
    async def mock_execute_stream_cancel(request, task_id=None):
        cancellation_token.set()
        yield StreamEvent(event_type=StreamEventType.TOOL_COMPLETE, content="done")
        
    software_engineer.streaming_engine.execute_stream = mock_execute_stream_cancel

    events = []
    async def run_test():
        async for event in software_engineer.execute_feature("Build feature X", cancellation_token=cancellation_token):
            if isinstance(event, EngineerEvent):
                events.append(event.event_type)
    asyncio.run(run_test())

    assert "feature_cancelled" in events
    # Ensure complete_task wasn't called for the cancelled task
    assert software_engineer.task_manager.complete_task.call_count == 0
    assert software_engineer.task_manager.cancel_task.call_count == 1
