"""Derive UI-friendly status from LangGraph snapshots and list threads from Postgres."""

from __future__ import annotations

from typing import Any

INTERRUPT = "__interrupt__"


def summarize_run(
    thread_id: str,
    snap: Any,
    error: str | None,
    *,
    started_at: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable summary for /runs and Streamlit filters."""
    raw_vals = getattr(snap, "values", None)
    if raw_vals is None:
        values = {}
    elif isinstance(raw_vals, dict):
        values = dict(raw_vals)
    else:
        values = {}
    next_nodes = list(snap.next) if snap.next else []
    next_set = set(next_nodes)

    topic = str(values.get("topic") or "")
    project = str(values.get("project_name") or "")
    approval_raw = values.get("approval_status")
    approval_display = (
        str(approval_raw).strip().lower()
        if approval_raw is not None and str(approval_raw).strip()
        else ""
    )
    err_lower = (error or "").lower()
    if approval_display == "cancelled":
        approval_state = "cancelled"
    elif error and "cancelled by user" in err_lower:
        approval_state = "cancelled"
    elif approval_display in ("approved", "rejected"):
        approval_state = approval_display
    elif "human_approval" in next_set or values.get(INTERRUPT):
        approval_state = "pending"
    else:
        approval_state = "not_reached"

    build_logs = str(values.get("build_logs") or "")
    has_build_output = bool(build_logs.strip())

    if error:
        if "cancelled by user" in err_lower:
            task_status = "cancelled"
        else:
            task_status = "failed"
    elif approval_display == "cancelled":
        task_status = "cancelled"
    elif approval_display == "rejected":
        task_status = "rejected"
    elif "human_approval" in next_set or values.get(INTERRUPT):
        task_status = "waiting_approval"
    elif "builder_agent" in next_set:
        task_status = "building"
    elif not next_set:
        task_status = "completed"
    else:
        task_status = "running"

    if error and "builder" in (error or "").lower():
        build_state = "failed"
    elif "builder_agent" in next_set:
        build_state = "running"
    elif has_build_output:
        build_state = "done"
    elif approval_state == "approved":
        build_state = "pending"
    else:
        build_state = "none"

    meta_ts = None
    if snap.metadata and isinstance(snap.metadata, dict):
        meta_ts = snap.metadata.get("created_at") or snap.metadata.get("ts")

    return {
        "thread_id": thread_id,
        "topic": topic,
        "project_name": project,
        "task_status": task_status,
        "approval_state": approval_state,
        "build_state": build_state,
        "next_nodes": next_nodes,
        "quality_passed": values.get("quality_passed"),
        "revision_count": values.get("revision_count"),
        "max_iterations_before_approval": values.get("max_iterations_before_approval"),
        "has_error": bool(error),
        "error_preview": (error[:200] + "…") if error and len(error) > 200 else error,
        "checkpoint_time": meta_ts,
        "started_at": started_at,
    }


def passes_filters(
    row: dict[str, Any],
    *,
    task_status: str | None,
    approval_state: str | None,
    build_state: str | None,
) -> bool:
    if task_status and row.get("task_status") != task_status:
        return False
    if approval_state and row.get("approval_state") != approval_state:
        return False
    if build_state and row.get("build_state") != build_state:
        return False
    return True


def list_thread_ids_from_postgres(conn_str: str, *, limit: int = 500) -> list[str]:
    """LangGraph thread_ids from checkpoint rows (default namespace)."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(conn_str, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # GROUP BY avoids DISTINCT+ORDER BY edge cases across Postgres versions.
            cur.execute(
                """
                SELECT thread_id
                FROM checkpoints
                WHERE checkpoint_ns = %s
                GROUP BY thread_id
                ORDER BY thread_id DESC
                LIMIT %s
                """,
                ("", limit),
            )
            return [str(r["thread_id"]) for r in cur.fetchall()]
