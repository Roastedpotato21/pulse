from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pulse.core.agent import AgentRequest, AgentResponse
from pulse.mutations import MutationTracker
from pulse.orchestration import AgentOrchestrator
from pulse.safety import SafetyManager
from pulse.verification import VerificationEngine, VerificationResult


@dataclass
class LoopStepState:
    step: int
    prompt: str
    response_content: str
    tool_name: str | None
    safety_approved: bool
    verification_success: bool | None
    mutations_count: int


@dataclass
class LoopResult:
    success: bool
    turns: int
    final_response: str
    history: list[LoopStepState] = field(default_factory=list)
    verification: VerificationResult | None = None


class AutonomousLoop:
    """Manages multi-step autonomous execution loop for Pulse.

    Loop steps:
    1. Evaluate step & execute via AgentOrchestrator
    2. Check safety via SafetyManager
    3. Run verification via VerificationEngine
    4. Track file system mutations via MutationTracker
    5. Checkpoint state
    """

    def __init__(
        self,
        orchestrator: AgentOrchestrator,
        safety_manager: SafetyManager | None = None,
        verification_engine: VerificationEngine | None = None,
        mutation_tracker: MutationTracker | None = None,
        checkpoint_dir: Path | None = None,
        max_steps: int = 5,
    ) -> None:
        self.orchestrator = orchestrator
        self.safety_manager = safety_manager or getattr(orchestrator, "safety_manager", None) or SafetyManager()
        self.verification_engine = verification_engine
        self.mutation_tracker = mutation_tracker
        self.checkpoint_dir = checkpoint_dir
        self.max_steps = max_steps

    async def run(self, initial_prompt: str) -> LoopResult:
        step_history: list[LoopStepState] = []
        current_prompt = initial_prompt
        last_response: AgentResponse | None = None
        last_verification: VerificationResult | None = None

        for turn in range(1, self.max_steps + 1):
            # 1. Evaluate step & execute via AgentOrchestrator
            request = AgentRequest(message=current_prompt, metadata={"step": turn})
            response = await self.orchestrator.handle_request(request)
            last_response = response

            # 2. Check safety via SafetyManager
            tool_name = response.tool_name
            safety_approved = True
            if tool_name:
                safety_approved = await self.safety_manager.authorize(
                    action=tool_name,
                    target=current_prompt,
                    detail=f"Turn {turn} tool execution: {tool_name}",
                )

            # 3. Track mutations & verify via VerificationEngine
            mutations_count = 0
            verification_ok: bool | None = None

            if self.mutation_tracker:
                events = self.mutation_tracker.latest_transaction()
                mutations_count = len(events)

            if self.verification_engine:
                last_verification = await self.verification_engine.verify()
                verification_ok = last_verification.success

            step_state = LoopStepState(
                step=turn,
                prompt=current_prompt,
                response_content=response.content,
                tool_name=tool_name,
                safety_approved=safety_approved,
                verification_success=verification_ok,
                mutations_count=mutations_count,
            )
            step_history.append(step_state)

            # 4. Checkpoint state
            self.checkpoint_state(turn, step_history)

            # Termination conditions
            if not tool_name or not safety_approved or (verification_ok is True):
                return LoopResult(
                    success=safety_approved and (verification_ok is not False),
                    turns=turn,
                    final_response=response.content,
                    history=step_history,
                    verification=last_verification,
                )

            current_prompt = f"Previous step output ({tool_name}): {response.content}\nContinue task execution."

        return LoopResult(
            success=False,
            turns=self.max_steps,
            final_response=last_response.content if last_response else "Max steps limit reached.",
            history=step_history,
            verification=last_verification,
        )

    def checkpoint_state(self, step: int, history: list[LoopStepState]) -> None:
        if not self.checkpoint_dir:
            return
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_file = self.checkpoint_dir / f"checkpoint_step_{step}.json"
        data = {
            "step": step,
            "history": [asdict(item) for item in history],
        }
        checkpoint_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
