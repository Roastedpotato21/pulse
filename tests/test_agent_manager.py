import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pulse.agent_manager import (
    AgentManager,
    AgentMessage,
    CollaborationAgent,
    CollaborationContext,
    DocumentationAgent,
    GitAgent,
    PlannerAgent,
    ReviewerAgent,
    SoftwareEngineerAgent,
    TaskCategory,
    TestingAgent,
)
from pulse.providers.base import ProviderRequestError
from pulse.streaming import CancellationToken, StreamEvent, StreamEventType
from pulse.task_manager import Task, TaskManager, TaskStatus


class MockCollaborationAgent(CollaborationAgent):
    def __init__(self, name: str, capabilities: list[TaskCategory]):
        self.name = name
        self.capabilities = capabilities
        self.execute_called = False
        
    async def execute(self, task: Task, shared_context: CollaborationContext, token: CancellationToken):
        self.execute_called = True
        yield StreamEvent(event_type=StreamEventType.TOOL_COMPLETE, content=f"[{self.name}] done")


@pytest.fixture
def mock_task_manager():
    tm = MagicMock()
    
    t1 = Task(id="1", title="Plan things", goal="Make a plan", status=TaskStatus.QUEUED)
    t2 = Task(id="2", title="Write code", goal="Implement the plan", status=TaskStatus.QUEUED)
    
    def mock_list_tasks(status=None):
        return [t for t in [t1, t2] if t.status == status or status is None]
        
    def mock_get_task(tid):
        if tid == "1": return t1
        if tid == "2": return t2
        return None
        
    tm.list_tasks = MagicMock(side_effect=mock_list_tasks)
    tm.get_task = MagicMock(side_effect=mock_get_task)
    
    def mock_update_status(tid, status):
        (t1 if tid == "1" else t2).status = status
        
    tm.start_task = AsyncMock(side_effect=lambda tid: mock_update_status(tid, TaskStatus.RUNNING))
    tm.complete_task = AsyncMock(side_effect=lambda tid, res="": setattr(t1 if tid == "1" else t2, "status", TaskStatus.COMPLETED))
    tm.fail_task = AsyncMock(side_effect=lambda tid, err: setattr(t1 if tid == "1" else t2, "status", TaskStatus.FAILED))
    async def mock_execute_task(tid, worker):
        task = t1 if tid == "1" else t2
        mock_update_status(tid, TaskStatus.RUNNING)
        await worker(task)
        task.status = TaskStatus.COMPLETED
        return task
    tm.execute_task = AsyncMock(side_effect=mock_execute_task)
    
    # We also need to mock create_task and queue_task in case they are used
    async def mock_create_task(**kwargs):
        return Task(id="3", title=kwargs.get('title', ''), goal=kwargs.get('goal', ''), status=TaskStatus.PENDING)
    tm.create_task = AsyncMock(side_effect=mock_create_task)
    tm.queue_task = AsyncMock()
    return tm


def test_agent_manager_dynamic_assignment():
    tm = MagicMock()
    agent1 = MockCollaborationAgent("A", [TaskCategory.CODING])
    agent2 = MockCollaborationAgent("B", [TaskCategory.TESTING])
    
    manager = AgentManager(task_manager=tm, agents=[agent1, agent2])
    
    task_code = Task(id="1", title="Implement feature", goal="write code for feature")
    assert manager._select_agent_for_task(task_code).name == "A"
    
    task_test = Task(id="2", title="Verify feature", goal="write test for feature")
    assert manager._select_agent_for_task(task_test).name == "B"


def test_agent_manager_parallel_execution(mock_task_manager):
    agent1 = MockCollaborationAgent("Planner", [TaskCategory.PLANNING])
    agent2 = MockCollaborationAgent("Coder", [TaskCategory.CODING])
    manager = AgentManager(task_manager=mock_task_manager, agents=[agent1, agent2])

    events = []
    
    async def run_test():
        async for evt in manager.execute_task_graph():
            events.append(evt)

    asyncio.run(run_test())
    
    print("DEBUG EVENTS:", events)
    
    # Ensure both agents were called
    assert agent1.execute_called
    assert agent2.execute_called
    
    # Ensure completion events were yielded
    event_contents = [e.content for e in events]
    assert "[Planner] done" in event_contents
    assert "[Coder] done" in event_contents
    
    # Task manager should have been updated
    assert mock_task_manager.execute_task.call_count == 2

def test_agent_manager_run_response(mock_task_manager):
    # Setup agent that actually posts a message to shared_context
    class RespondingAgent(CollaborationAgent):
        def __init__(self):
            self.name = "Responder"
            self.capabilities = [TaskCategory.GENERAL]
            
        async def execute(self, task, shared_context, token):
            shared_context.post_message(AgentMessage(
                sender=self.name,
                recipient=None,
                category=TaskCategory.GENERAL,
                content="This is the generated explanation."
            ))
            yield StreamEvent(event_type=StreamEventType.TOOL_COMPLETE, content="done")

    async def run_test():
        manager = AgentManager(task_manager=mock_task_manager, agents=[RespondingAgent()])
        result = await manager.run("Explain how it works", ["System context block"])
        
        # The run result should be exactly the agent's message, not the system context
        assert result.final_response == "This is the generated explanation."

    asyncio.run(run_test())


def test_agent_manager_surfaces_non_retryable_provider_failure_without_stale_answer(
    tmp_path,
):
    class BillingFailureAgent(CollaborationAgent):
        def __init__(self):
            self.name = "Responder"
            self.capabilities = [TaskCategory.GENERAL]

        async def execute(self, task, shared_context, token):
            message = (
                "Model request failed (openrouter HTTP 402): "
                "The openrouter account has insufficient credits or paid-model access."
            )
            yield StreamEvent(event_type=StreamEventType.TOOL_FAILED, content=message)
            raise ProviderRequestError(message, status_code=402, retryable=False)

    async def run_test():
        task_manager = TaskManager(tmp_path)
        manager = AgentManager(task_manager=task_manager, agents=[BillingFailureAgent()])
        manager.shared_context.post_message(
            AgentMessage("Responder", None, TaskCategory.GENERAL, "stale answer")
        )

        with pytest.raises(RuntimeError, match="HTTP 402"):
            await manager.run("Explain an API", [])

        failed = task_manager.list_tasks(status=TaskStatus.DEAD_LETTER)
        assert len(failed) == 1
        assert failed[0].retries == 1

    asyncio.run(run_test())



def test_specialized_agents_initialization():
    provider = MagicMock()
    planner = PlannerAgent(provider)
    coder = SoftwareEngineerAgent(provider)
    reviewer = ReviewerAgent(provider)
    tester = TestingAgent(provider)
    doc = DocumentationAgent(provider)
    git = GitAgent(provider)
    
    assert TaskCategory.PLANNING in planner.capabilities
    assert TaskCategory.CODING in coder.capabilities
    assert TaskCategory.REVIEW in reviewer.capabilities
    assert TaskCategory.TESTING in tester.capabilities
    assert TaskCategory.DOCUMENTATION in doc.capabilities
    assert TaskCategory.GIT in git.capabilities


def test_collaboration_context():
    ctx = CollaborationContext()
    ctx.post_message(AgentMessage("Planner", None, TaskCategory.PLANNING, "Plan ready"))
    ctx.post_message(AgentMessage("Coder", None, TaskCategory.CODING, "Code ready"))
    
    msgs = ctx.get_messages()
    assert len(msgs) == 2
    
    coding_msgs = ctx.get_messages(TaskCategory.CODING)
    assert len(coding_msgs) == 1
    assert coding_msgs[0].sender == "Coder"
    
    formatted = ctx.format_for_prompt()
    assert "Plan ready" in formatted
    assert "Code ready" in formatted
