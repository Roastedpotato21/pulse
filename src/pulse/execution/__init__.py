"""Execution adapters that bind durable tasks to isolated backends."""

from pulse.execution.remote_task import RemoteTaskExecutor

__all__ = ["RemoteTaskExecutor"]
