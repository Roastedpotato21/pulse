"""Unit tests for Phase 2 of Pulse Sandbox: Resource Limits and Process Management."""

import sys

import pytest

from pulse.sandbox.process import ProcessManager
from pulse.sandbox.resources import ResourceLimiter, ResourceLimits

# ---------------------------------------------------------------------------
# 1. Resource Limiter Tests
# ---------------------------------------------------------------------------


def test_resource_limiter_output_truncation():
    limits = ResourceLimits(max_output_bytes=50)
    limiter = ResourceLimiter(limits)

    small_text = "Short output text."
    clean, truncated = limiter.truncate_output(small_text)
    assert clean == small_text
    assert truncated is False

    large_text = "A" * 100
    clean, truncated = limiter.truncate_output(large_text)
    assert truncated is True
    assert "[OUTPUT TRUNCATED BY SANDBOX RESOURCE LIMITER]" in clean


# ---------------------------------------------------------------------------
# 2. Process Manager Execution Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_process_manager_successful_execution():
    pm = ProcessManager()
    code = "import sys; print('SANDBOX_OUTPUT_TEST'); sys.exit(0)"
    result = await pm.execute([sys.executable, "-c", code])

    assert result.exit_code == 0
    assert "SANDBOX_OUTPUT_TEST" in result.stdout
    assert result.timed_out is False
    assert result.duration_ms > 0


@pytest.mark.anyio
async def test_process_manager_timeout_enforcement():
    pm = ProcessManager()
    limits = ResourceLimits(timeout_seconds=0.5)

    code = "import time; time.sleep(5.0)"
    result = await pm.execute([sys.executable, "-c", code], limits=limits)

    assert result.timed_out is True
    assert result.exit_code == -9
    assert "timed out" in result.stderr.lower()


@pytest.mark.anyio
async def test_process_manager_preserves_output_before_timeout():
    pm = ProcessManager()
    limits = ResourceLimits(timeout_seconds=0.5)

    code = (
        "import time; "
        "print('OUTPUT_BEFORE_TIMEOUT', flush=True); "
        "time.sleep(5.0)"
    )
    result = await pm.execute([sys.executable, "-c", code], limits=limits)

    assert result.timed_out is True
    assert "OUTPUT_BEFORE_TIMEOUT" in result.stdout


@pytest.mark.anyio
async def test_process_manager_output_size_cap():
    pm = ProcessManager()
    limits = ResourceLimits(max_output_bytes=100)

    code = "print('X' * 500)"
    result = await pm.execute([sys.executable, "-c", code], limits=limits)

    assert result.truncated is True
    assert "OUTPUT TRUNCATED" in result.stdout
