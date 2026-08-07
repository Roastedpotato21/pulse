"""Evaluation utilities for Pulse.
Provides:
- PatchVerifier: Apply candidate diffs and evaluate test outcomes.
- TrajectoryLogger: Record agent action trajectories.
"""

from .trajectory_logger import TrajectoryLogger, TrajectoryStep  # noqa: F401
from .verifier import PatchVerifier  # noqa: F401
