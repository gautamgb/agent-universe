"""Streamlit dashboard: all runs, filters, and per-thread detail (state, logs, approve)."""

from __future__ import annotations

import os
import time
import math
from typing import Any

import httpx
import streamlit as st

DEFAULT_API = "http://127.0.0.1:8888"
DEFAULT_MAX_ITERATIONS = int(os.environ.get("QUALITY_MAX_REVISIONS", "5"))

TASK_OPTIONS = [
    "",
    "failed",
    "rejected",
    "cancelled",
    "waiting_approval",
    "building",
    "running",
    "completed",
]
APPROVAL_OPTIONS = ["", "pending", "approved", "rejected", "cancelled", "not_reached"]
BUILD_OPTIONS = ["", "none", "pending", "running", "done", "failed"]


def _base() -> str:
    return os.environ.get("AGENT_UNIVERSE_API_BASE", DEFAULT_API).rstrip("/")


def _terminal_task_status(status: str | None) -> bool:
    return (status or "") in ("cancelled", "completed", "rejected", "failed")


def _stopped_for_recovery(
    values: dict[str, Any],
    err: str | None,
    detail_row: dict[str, Any] | None,
) -> bool:
    """Run is finished after cancel (human gate or cooperative early)."""
    if (values.get("approval_status") or "").lower() == "cancelled":
        return True
    if err and "cancelled by user" in err.lower():
        return True
    if detail_row and detail_row.get("task_status") == "cancelled":
        return True
    return False


def _pending_human_approval(values: dict[str, Any], next_nodes: list[Any]) -> bool:
    """True when the graph is paused at human_approval (interrupt), not while scout/quality loop runs."""
    next_set = {str(x) for x in next_nodes}
    if "human_approval" in next_set:
        return True
    if values.get("__interrupt__"):
        return True
    return False


def _approval_display(values: dict[str, Any], next_nodes: list[Any]) -> str:
    status = (values.get("approval_status") or "").strip().lower()
    if status:
        return status
    if _pending_human_approval(values, next_nodes):
        return "pending"
    return "not_reached"


def _sort_value(run: dict[str, Any], key: str) -> Any:
    if key in {"started_at", "checkpoint_time"}:
        return str(run.get(key) or "")
    return str(run.get(key) or "").lower()


def _matches_filters(
    run: dict[str, Any],
    *,
    task_status: str | None,
    approval_state: str | None,
    build_state: str | None,
) -> bool:
    if task_status and (run.get("task_status") or "") != task_status:
        return False
    if approval_state and (run.get("approval_state") or "") != approval_state:
        return False
    if build_state and (run.get("build_state") or "") != build_state:
        return False
    return True


def _fetch_runs(
    *,
    task_status: str | None,
    approval_state: str | None,
    build_state: str | None,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Returns (runs, error_message, degraded_warning_from_api)."""
    params: dict[str, str] = {}
    if task_status:
        params["task_status"] = task_status
    if approval_state:
        params["approval_state"] = approval_state
    if build_state:
        params["build_state"] = build_state
    try:
        r = httpx.get(f"{_base()}/runs", params=params or None, timeout=60.0)
        r.raise_for_status()
        data = r.json()
        return data.get("runs") or [], None, (data.get("warning") or None)
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            detail = str(body.get("detail", e.response.text))
        except Exception:
            detail = (e.response.text or "").strip() or str(e)
        return [], f"HTTP {e.response.status_code}: {detail}", None
    except Exception as e:
        return [], str(e), None


st.set_page_config(page_title="Agent Universe", layout="wide")
st.title("Agent Universe — Factory Console")
live_status_slot = st.container()

if "thread_id" not in st.session_state:
    st.session_state.thread_id = None
if "auto_refresh" not in st.session_state:
    st.session_state.auto_refresh = True

# --- Start new run ---
col_a, col_b = st.columns([2, 1])
with col_a:
    with st.form("start"):
        topic = st.text_input("Research / build topic", value="Secure internal API for identity sync")
        project = st.text_input("Project folder under projects/", value="demo")
        max_iters = st.number_input(
            "Max architect/quality iterations before human approval",
            min_value=1,
            max_value=50,
            value=max(1, min(50, DEFAULT_MAX_ITERATIONS)),
            step=1,
        )
        go = st.form_submit_button("Start new run")
        if go:
            try:
                r = httpx.post(
                    f"{_base()}/runs",
                    json={
                        "topic": topic,
                        "project_name": project,
                        "max_iterations_before_approval": int(max_iters),
                    },
                    timeout=60.0,
                )
                r.raise_for_status()
                st.session_state.thread_id = r.json()["thread_id"]
                st.success(f"Started thread `{st.session_state.thread_id}`")
            except Exception as e:
                st.error(str(e))

with col_b:
    st.session_state.auto_refresh = st.toggle("Auto-refresh detail (2s)", value=st.session_state.auto_refresh)

# --- Filters + run catalog ---
st.subheader("Workstreams & projects")
f1, f2, f3, f4, f5 = st.columns(5)
with f1:
    ft_task = st.selectbox("Task status", TASK_OPTIONS, format_func=lambda x: "All" if x == "" else x)
with f2:
    ft_appr = st.selectbox("Approval state", APPROVAL_OPTIONS, format_func=lambda x: "All" if x == "" else x)
with f3:
    ft_build = st.selectbox("Build state", BUILD_OPTIONS, format_func=lambda x: "All" if x == "" else x)
with f4:
    sort_field = st.selectbox(
        "Sort by",
        (
            "started_at",
            "checkpoint_time",
            "revision_count",
            "max_iterations_before_approval",
            "task_status",
            "approval_state",
            "build_state",
            "project_name",
            "topic",
        ),
        format_func=lambda s: s.replace("_", " ").title(),
    )
with f5:
    sort_desc = st.toggle("Desc", value=True)

p1, p2 = st.columns(2)
with p1:
    page_size = st.selectbox("Rows per page", (5, 10, 20, 50), index=1)

runs_all, err_list, runs_warning = _fetch_runs(task_status=None, approval_state=None, build_state=None)

with live_status_slot:
    st.subheader("Live work in progress")
    if err_list:
        st.caption(f"Live status unavailable: {err_list}")
    else:
        active_runs = [r for r in runs_all if not _terminal_task_status(r.get("task_status"))]
        running = sum(1 for r in active_runs if (r.get("task_status") or "") == "running")
        building = sum(1 for r in active_runs if (r.get("task_status") or "") == "building")
        waiting = sum(1 for r in active_runs if (r.get("task_status") or "") == "waiting_approval")
        with_worker = sum(1 for r in active_runs if r.get("in_flight"))
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Active workstreams", len(active_runs))
        m2.metric("Running", running)
        m3.metric("Building", building)
        m4.metric("Waiting approval", waiting)

        if active_runs:
            live_rows: list[dict[str, Any]] = []
            for r in sorted(active_runs, key=lambda x: _sort_value(x, "checkpoint_time"), reverse=True)[:8]:
                live_rows.append(
                    {
                        "thread_id": str(r.get("thread_id") or "")[:8],
                        "project": r.get("project_name") or "",
                        "task": r.get("task_status") or "",
                        "approval": r.get("approval_state") or "",
                        "build": r.get("build_state") or "",
                        "step": ", ".join(r.get("next_nodes") or [])[:60] or "(active)",
                        "worker": r.get("worker_kind") or ("active" if r.get("in_flight") else ""),
                        "iter": f"{r.get('revision_count') or 0}/{r.get('max_iterations_before_approval') or DEFAULT_MAX_ITERATIONS}",
                        "topic": (r.get("topic") or "")[:70],
                    }
                )
            st.dataframe(live_rows, use_container_width=True, hide_index=True)
            st.caption(
                f"Workers in flight: {with_worker}. Showing top {len(live_rows)} active runs by recent checkpoint."
            )
        else:
            st.success("No active workstreams right now.")

runs = [
    r
    for r in runs_all
    if _matches_filters(
        r,
        task_status=ft_task or None,
        approval_state=ft_appr or None,
        build_state=ft_build or None,
    )
]
if runs_warning:
    st.warning(runs_warning)
if err_list:
    st.warning(f"Could not list runs: {err_list}")
elif runs:
    sorted_runs = sorted(runs, key=lambda r: _sort_value(r, sort_field), reverse=sort_desc)
    total_rows = len(sorted_runs)
    total_pages = max(1, math.ceil(total_rows / page_size))
    with p2:
        page = int(
            st.number_input(
                "Page",
                min_value=1,
                max_value=total_pages,
                value=1,
                step=1,
            )
        )
    start = (page - 1) * page_size
    end = start + page_size
    page_runs = sorted_runs[start:end]

    st.caption(
        "Cancel requests cooperative stop (next safe point, or ends a pending approval). "
        f"Showing {start + 1}-{min(end, total_rows)} of {total_rows}."
    )
    for r in page_runs:
        tid_row = str(r.get("thread_id") or "")
        row1, row2 = st.columns([5, 1])
        with row1:
            one_row = [
                {
                    "thread_id": tid_row,
                    "project": r.get("project_name", ""),
                    "topic": (r.get("topic") or "")[:80],
                    "task": r.get("task_status", ""),
                    "approval": r.get("approval_state", ""),
                    "build": r.get("build_state", ""),
                    "stalled": "yes" if r.get("stalled") else "",
                    "in_flight": "yes" if r.get("in_flight") else "",
                    "worker": r.get("worker_kind") or "",
                    "iter_done": r.get("revision_count", ""),
                    "max_iter": r.get("max_iterations_before_approval", ""),
                    "next": ", ".join(r.get("next_nodes") or [])[:60],
                    "error": "yes" if r.get("has_error") else "",
                }
            ]
            st.dataframe(one_row, use_container_width=True, hide_index=True)
        with row2:
            if st.button(
                "Cancel",
                key=f"cancel_list_{tid_row}",
                disabled=_terminal_task_status(r.get("task_status")),
            ):
                try:
                    cr = httpx.post(f"{_base()}/runs/{tid_row}/cancel", timeout=60.0)
                    cr.raise_for_status()
                    st.success("Cancel requested.")
                except Exception as e:
                    st.error(str(e))
else:
    st.info("No runs match the filters (or no checkpoints yet).")

# Pick thread for detail panel (include focused id even if not yet in /runs)
_run_index = {r.get("thread_id"): r for r in runs if r.get("thread_id")}


def _thread_label(tid_opt: str | None) -> str:
    if tid_opt is None:
        return "— None —"
    r = _run_index.get(tid_opt)
    topic = ((r or {}).get("topic") or "")[:50]
    return f"{str(tid_opt)[:8]}… — {topic}" if topic else f"{str(tid_opt)[:8]}…"


thread_options: list[str | None] = [None]
seen: set[str] = set()
for candidate in [st.session_state.thread_id, *[r.get("thread_id") for r in runs]]:
    if candidate and candidate not in seen:
        seen.add(candidate)
        thread_options.append(candidate)

manual = st.selectbox(
    "Focus thread (detail below)",
    thread_options,
    format_func=_thread_label,
    key="focus_thread_select",
)
if manual is not None:
    st.session_state.thread_id = manual

tid = st.session_state.thread_id
if not tid:
    st.info("Select a thread above, or start a new run.")
    if st.session_state.auto_refresh:
        time.sleep(2.0)
        st.rerun()
    st.stop()

st.divider()
st.subheader(f"Detail — `{tid}`")

try:
    st_res = httpx.get(f"{_base()}/runs/{tid}/state", timeout=30.0)
    st_res.raise_for_status()
    payload = st_res.json()
except Exception as e:
    st.error(f"State fetch failed: {e}")
    if st.session_state.auto_refresh:
        time.sleep(2.0)
        st.rerun()
    st.stop()

values = payload.get("values") or {}
err = payload.get("error")
next_nodes = payload.get("next") or []
activity = payload.get("run_activity") or {}
storage = payload.get("storage") or {}

if err:
    st.error(err)
rev_done = int(values.get("revision_count") or 0)
max_iters = int(values.get("max_iterations_before_approval") or DEFAULT_MAX_ITERATIONS)
remaining_iters = max(0, max_iters - rev_done)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Iterations done", rev_done)
c2.metric("Quality pass", "yes" if values.get("quality_passed") else "no")
c3.metric("Approval", _approval_display(values, next_nodes))
c4.metric("Max iterations", max_iters)
c5.metric("Iterations remaining", remaining_iters)

if activity.get("stalled"):
    mode = "worker" if activity.get("in_flight") else "checkpoint"
    running_for = activity.get("worker_running_for_s")
    age = activity.get("checkpoint_age_s")
    st.warning(
        f"Run appears stalled ({mode}). "
        f"worker_running_for_s={running_for}, checkpoint_age_s={age}. "
        "Use Recover stalled run."
    )
elif activity.get("in_flight"):
    st.info(
        f"Run worker active: kind={activity.get('worker_kind')}, "
        f"running_for_s={activity.get('worker_running_for_s')}."
    )

with st.expander("Research", expanded=False):
    st.markdown(values.get("research") or "_empty_")
with st.expander("PM spec", expanded=False):
    st.markdown(values.get("pm_spec") or "_empty_")
with st.expander("Architecture", expanded=False):
    st.markdown(values.get("architecture_spec") or "_empty_")

st.subheader("Live build terminal")
try:
    lr = httpx.get(f"{_base()}/runs/{tid}/logs", timeout=30.0)
    lr.raise_for_status()
    logs = lr.json().get("build_logs") or ""
except Exception as e:
    logs = f"(log fetch failed: {e})"

st.code(logs or "(no builder output yet)", language="bash")

with st.expander("Storage locations", expanded=False):
    st.markdown(f"- Container project dir: `{storage.get('container_project_dir') or 'n/a'}`")
    st.markdown(f"- Container artifacts dir: `{storage.get('container_artifacts_dir') or 'n/a'}`")
    host_proj = storage.get("host_project_dir")
    host_art = storage.get("host_artifacts_dir")
    if host_proj:
        st.markdown(f"- Host project dir: `{host_proj}`")
        st.markdown(f"- Host artifacts dir: `{host_art}`")
        st.markdown(f"- Host project link: [open folder](file://{host_proj})")
        st.markdown(f"- Host artifacts link: [open folder](file://{host_art})")
    st.caption("PM/research/architecture artifacts are written under `.agent-universe/<thread_id>/`.")

st.caption(
    "Next nodes: "
    + (", ".join(str(x) for x in next_nodes) if next_nodes else "(idle or finished)")
)

can_approve_reject = _pending_human_approval(values, next_nodes)
if not can_approve_reject:
    rev = int(values.get("revision_count") or 0)
    cap = int(values.get("max_iterations_before_approval") or DEFAULT_MAX_ITERATIONS)
    st.caption(
        "Approve and Reject are enabled only when this run is **waiting at human approval** "
        f"(after quality gate pass or max revisions). Current progress: {rev}/{cap} revisions."
    )

_detail_row = _run_index.get(tid)
_can_cancel_detail = not (
    _detail_row is not None and _terminal_task_status(_detail_row.get("task_status"))
)

ap1, ap2, ap3, ap4, ap5 = st.columns(5)
if ap1.button("Approve & run builder", type="primary", disabled=not can_approve_reject):
    try:
        httpx.post(f"{_base()}/runs/{tid}/approve", json={"approved": True}, timeout=60.0)
        st.success("Resume queued (approved).")
    except Exception as e:
        st.error(str(e))
if ap2.button("Reject", disabled=not can_approve_reject):
    try:
        httpx.post(f"{_base()}/runs/{tid}/approve", json={"approved": False}, timeout=60.0)
        st.warning("Resume queued (rejected).")
    except Exception as e:
        st.error(str(e))
if ap3.button("Cancel run", disabled=not _can_cancel_detail):
    try:
        cr = httpx.post(f"{_base()}/runs/{tid}/cancel", timeout=60.0)
        cr.raise_for_status()
        st.warning("Cancel requested.")
    except Exception as e:
        st.error(str(e))
if ap4.button("Clear focus thread"):
    st.session_state.thread_id = None
    st.rerun()
if ap5.button("Recover stalled run"):
    try:
        rr = httpx.post(f"{_base()}/runs/{tid}/recover", timeout=60.0)
        rr.raise_for_status()
        msg = (rr.json() or {}).get("status") or "recover_queued"
        if msg == "awaiting_human_approval":
            st.info("Run is already at human approval; use Approve/Reject/Cancel.")
        elif msg == "idle_or_completed":
            st.info("Run has no pending steps to recover.")
        else:
            st.success("Recovery queued.")
    except Exception as e:
        st.error(str(e))

if _stopped_for_recovery(values, err, _detail_row):
    with st.expander("Cancelled or stopped — next steps", expanded=True):
        st.caption(
            "**Resume run** clears checkpoints and starts the workflow again with the same topic and project. "
            "**Delete run** removes this thread from the console and checkpoint database."
        )
        rx1, rx2 = st.columns(2)
        with rx1:
            if st.button("Resume run", key=f"resume_after_{tid}"):
                try:
                    rr = httpx.post(f"{_base()}/runs/{tid}/restart", timeout=60.0)
                    rr.raise_for_status()
                    st.success("Restart queued — same thread id; watch progress.")
                except Exception as e:
                    st.error(str(e))
        with rx2:
            if st.button("Delete run", key=f"delete_after_{tid}"):
                try:
                    dr = httpx.delete(f"{_base()}/runs/{tid}", timeout=60.0)
                    dr.raise_for_status()
                    st.success("Run deleted.")
                    st.session_state.thread_id = None
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

if st.session_state.auto_refresh:
    time.sleep(2.0)
    st.rerun()
