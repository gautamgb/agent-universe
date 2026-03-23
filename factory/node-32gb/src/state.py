"""Shared graph state for the factory workflow."""

from __future__ import annotations

from typing import Annotated, Optional, TypedDict


def append_logs(existing: Optional[str], new: Optional[str]) -> str:
    if not existing:
        return new or ""
    if not new:
        return existing
    return existing + new


class AgentState(TypedDict, total=False):
    """Checkpointed workflow state (Postgres)."""

    thread_id: str
    topic: str
    project_name: str
    research: str
    pm_spec: str
    architecture_spec: str
    quality_passed: bool
    quality_feedback: str
    revision_count: int
    max_iterations_before_approval: int
    build_logs: Annotated[str, append_logs]
    approval_status: Optional[str]
