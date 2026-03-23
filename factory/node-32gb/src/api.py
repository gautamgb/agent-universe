"""FastAPI service: start runs, resume human approval, expose state and build logs."""

from __future__ import annotations

import logging
import os
import threading
import traceback
import uuid
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from langgraph.types import Interrupt

from src.builder_agent import clear_build_buffer, get_build_buffer, kill_aider_process
from src.db_url import checkpoint_conn_str
from src.graph import build_app_graph
from src.run_catalog import list_thread_ids_from_postgres, passes_filters, summarize_run
from src.run_control import clear_cancelled, mark_cancelled

_root = Path(os.environ.get("AGENT_UNIVERSE_ROOT", os.getcwd()))
if _root.is_dir():
    load_dotenv(_root / ".env")
load_dotenv()


def _fill_missing_from_workspace_env() -> None:
    """If Docker Compose omitted vars (env_file quirks), read them from mounted repo .env."""
    path = _root / ".env"
    if not path.is_file():
        return
    merged = dotenv_values(path)
    for key in (
        "PRIMARY_MODEL",
        "SCOUT_MODEL",
        "LITELLM_MODEL",
        "LITELLM_BASE_URL",
        "LITELLM_MASTER_KEY",
        "LITELLM_TEMPERATURE",
        "LITELLM_API_KEY",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_CHECKPOINT_HOST",
        "TAILSCALE_16GB_IP",
        "TAILSCALE_32GB_IP",
        "CHECKPOINTER",
        "GEMINI_API_KEY",
        "OLLAMA_API_BASE",
        "AIDER_MODEL",
        "AIDER_DOCKER_IMAGE",
        "AIDER_OLLAMA_API_BASE",
        "QUALITY_MAX_REVISIONS",
        "AGENT_UNIVERSE_HOST_PATH",
        "SCOPED_GITHUB_TOKEN",
        "SLACK_WEBHOOK_URL",
        "SLACK_SIGNING_SECRET",
        "SLACK_ALLOWED_USER_IDS",
        "AGENT_UNIVERSE_DASHBOARD_URL",
    ):
        if (os.environ.get(key) or "").strip():
            continue
        val = merged.get(key)
        if val is not None and str(val).strip():
            os.environ[key] = str(val).strip()


def _sync_model_ids_from_workspace_env() -> None:
    """Always align PRIMARY_MODEL / SCOUT_MODEL with mounted repo .env when set there.

    Docker Compose may inject stale values (e.g. ollama/...:7b); _fill_missing only runs when empty,
    so wrong non-empty values would persist without this.
    """
    path = _root / ".env"
    if not path.is_file():
        return
    merged = dotenv_values(path)
    for key in ("PRIMARY_MODEL", "SCOUT_MODEL", "LITELLM_MODEL"):
        val = merged.get(key)
        if val is not None and str(val).strip():
            os.environ[key] = str(val).strip()


_fill_missing_from_workspace_env()
_sync_model_ids_from_workspace_env()
if not os.environ.get("LITELLM_API_KEY") and os.environ.get("LITELLM_MASTER_KEY"):
    os.environ["LITELLM_API_KEY"] = os.environ["LITELLM_MASTER_KEY"]


def _default_quality_max_revisions() -> int:
    raw = (os.environ.get("QUALITY_MAX_REVISIONS") or "5").strip()
    try:
        n = int(raw)
    except ValueError:
        return 5
    return max(1, n)


def _slack_ephemeral(text: str) -> dict[str, str]:
    return {"response_type": "ephemeral", "text": text}


def _slack_signing_secret() -> str:
    return (os.environ.get("SLACK_SIGNING_SECRET") or "").strip()


def _slack_allowed_user_ids() -> set[str]:
    raw = (os.environ.get("SLACK_ALLOWED_USER_IDS") or "").strip()
    if not raw:
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


def _verify_slack_signature(headers: dict[str, str], body: bytes) -> bool:
    secret = _slack_signing_secret()
    if not secret:
        return False
    ts = headers.get("x-slack-request-timestamp") or ""
    sig = headers.get("x-slack-signature") or ""
    if not ts or not sig:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    now = int(datetime.now(timezone.utc).timestamp())
    if abs(now - ts_int) > 300:
        return False
    base = f"v0:{ts}:{body.decode('utf-8')}".encode("utf-8")
    expected = "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _parse_slack_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


def _slack_user_allowed(user_id: str) -> bool:
    allowed = _slack_allowed_user_ids()
    if not allowed:
        return True
    return user_id in allowed


class StartRunBody(BaseModel):
    topic: str = Field(..., min_length=1)
    project_name: str = Field("default", min_length=1)
    max_iterations_before_approval: int = Field(default_factory=_default_quality_max_revisions, ge=1, le=50)


class ApproveBody(BaseModel):
    approved: bool = True


_errors: dict[str, str] = {}
_errors_lock = threading.Lock()
_graph_lock = threading.RLock()
_graph: Any = None
_checkpoint_setup_lock = threading.Lock()
_checkpoint_setup_succeeded_once = False
_checkpointer_cm: Any = None
_checkpoint_conn_str: str | None = None
_run_registry: dict[str, dict[str, Any]] = {}
_run_registry_lock = threading.Lock()
_run_workers: dict[str, dict[str, Any]] = {}
_run_workers_lock = threading.Lock()
_recover_attempts: dict[str, datetime] = {}
_recover_attempts_lock = threading.Lock()

_log = logging.getLogger(__name__)


def _register_run(tid: str, topic: str, project_name: str, max_iterations_before_approval: int) -> None:
    with _run_registry_lock:
        _run_registry[tid] = {
            "topic": topic,
            "project_name": project_name,
            "max_iterations_before_approval": max_iterations_before_approval,
            "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }


def _stall_seconds_threshold() -> int:
    raw = (os.environ.get("RUN_STALL_SECONDS") or "300").strip()
    try:
        n = int(raw)
    except ValueError:
        return 300
    return max(30, n)


def _worker_info(thread_id: str) -> dict[str, Any]:
    with _run_workers_lock:
        info = dict(_run_workers.get(thread_id) or {})
    if not info:
        return {"in_flight": False}
    started_at = info.get("started_at")
    elapsed = 0
    if isinstance(started_at, datetime):
        elapsed = int((datetime.now(timezone.utc) - started_at).total_seconds())
    out = {
        "in_flight": True,
        "worker_kind": info.get("kind"),
        "worker_started_at": started_at.isoformat() if isinstance(started_at, datetime) else None,
        "worker_running_for_s": elapsed,
    }
    threshold = _stall_seconds_threshold()
    out["stalled"] = elapsed >= threshold
    out["stall_threshold_s"] = threshold
    return out


def _start_run_worker(thread_id: str, kind: str, target: Any, *args: Any) -> bool:
    with _run_workers_lock:
        if thread_id in _run_workers:
            return False
        # Reserve slot before thread start to avoid double-start races.
        _run_workers[thread_id] = {
            "kind": kind,
            "started_at": datetime.now(timezone.utc),
        }

    def _wrapped() -> None:
        try:
            target(*args)
        finally:
            with _run_workers_lock:
                _run_workers.pop(thread_id, None)

    threading.Thread(target=_wrapped, daemon=True).start()
    return True


def _is_waiting_human_from_snapshot(snap: Any) -> bool:
    next_nodes = list(snap.next) if snap.next else []
    if "human_approval" in next_nodes:
        return True
    raw_vals = getattr(snap, "values", None)
    values: dict[str, Any] = dict(raw_vals) if isinstance(raw_vals, dict) else {}
    return bool(values.get("__interrupt__"))


def _storage_paths(project_name: str, thread_id: str) -> dict[str, Any]:
    root = Path(os.environ.get("AGENT_UNIVERSE_ROOT", os.getcwd())).resolve()
    project_dir = root / "projects" / project_name
    artifacts_dir = project_dir / ".agent-universe" / thread_id
    host_root = (os.environ.get("AGENT_UNIVERSE_HOST_PATH") or "").strip()
    host_project = (Path(host_root).resolve() / "projects" / project_name) if host_root else None
    host_artifacts = (host_project / ".agent-universe" / thread_id) if host_project else None
    return {
        "container_project_dir": str(project_dir),
        "container_artifacts_dir": str(artifacts_dir),
        "host_project_dir": str(host_project) if host_project else None,
        "host_artifacts_dir": str(host_artifacts) if host_artifacts else None,
    }


def _should_auto_recover(snap: Any, err: str | None, thread_id: str) -> bool:
    if err:
        return False
    if _is_waiting_human_from_snapshot(snap):
        return False
    next_nodes = list(snap.next) if snap.next else []
    if not next_nodes:
        return False
    with _run_workers_lock:
        if thread_id in _run_workers:
            return False
    age = _checkpoint_age_s(snap)
    if age is None:
        return False
    if age < _stall_seconds_threshold():
        return False
    with _recover_attempts_lock:
        last = _recover_attempts.get(thread_id)
        now = datetime.now(timezone.utc)
        if last and (now - last).total_seconds() < 60:
            return False
        _recover_attempts[thread_id] = now
    return True


def _set_error(tid: str, msg: str) -> None:
    with _errors_lock:
        _errors[tid] = msg


def _clear_error(tid: str) -> None:
    with _errors_lock:
        _errors.pop(tid, None)


def _make_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _thread_is_mid_execution(snap: Any, thread_id: str | None = None) -> bool:
    """True if the graph is actively running a node (not paused at human interrupt)."""
    next_nodes = list(snap.next) if snap.next else []
    if not next_nodes:
        return False
    raw_vals = getattr(snap, "values", None)
    values: dict[str, Any] = dict(raw_vals) if isinstance(raw_vals, dict) else {}
    if values.get("__interrupt__"):
        return False
    if thread_id:
        with _run_workers_lock:
            return thread_id in _run_workers
    return True


def _checkpoint_age_s(snap: Any) -> int | None:
    meta = getattr(snap, "metadata", None)
    if not isinstance(meta, dict):
        return None
    raw = meta.get("created_at") or meta.get("ts")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return max(0, int(datetime.now(timezone.utc).timestamp() - float(raw)))
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, int((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()))
        except Exception:
            return None
    return None


def _exception_chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = [exc]
    seen: set[int] = {id(exc)}
    c = exc.__cause__
    while c is not None and id(c) not in seen:
        out.append(c)
        seen.add(id(c))
        c = c.__cause__
    return out


def _is_missing_checkpoints_table_error(exc: BaseException) -> bool:
    try:
        from psycopg.errors import UndefinedTable
    except ImportError:
        UndefinedTable = ()  # type: ignore[misc, assignment]

    for e in _exception_chain(exc):
        if UndefinedTable and isinstance(e, UndefinedTable):
            return True
        msg = str(e).lower()
        if "42p01" in msg:
            return True
        if "checkpoints" in msg or "checkpoint_migrations" in msg:
            if "does not exist" in msg or "undefined" in msg:
                return True
    return False


def _maybe_run_checkpoint_setup_before_list() -> None:
    """Ensure LangGraph migrations ran (same DB the saver uses). Idempotent; retries until success."""
    global _checkpoint_setup_succeeded_once
    if os.environ.get("CHECKPOINTER", "postgres").lower() == "memory":
        return
    if _graph is None:
        return
    cp = getattr(_graph, "checkpointer", None)
    if cp is None or not hasattr(cp, "setup"):
        return
    with _checkpoint_setup_lock:
        if _checkpoint_setup_succeeded_once:
            return
        try:
            _log.info("Ensuring LangGraph checkpoint tables (checkpointer.setup before /runs list)")
            cp.setup()
            _checkpoint_setup_succeeded_once = True
        except Exception:
            _log.exception("checkpointer.setup() before list_runs failed")


def _ensure_checkpoint_tables_from_error(exc: BaseException) -> bool:
    """Run LangGraph migrations if Postgres error indicates missing checkpoint tables."""
    global _checkpoint_setup_succeeded_once
    if not _is_missing_checkpoints_table_error(exc):
        return False
    if os.environ.get("CHECKPOINTER", "postgres").lower() == "memory":
        return False
    if _graph is None:
        return False
    cp = getattr(_graph, "checkpointer", None)
    if cp is None or not hasattr(cp, "setup"):
        return False
    with _checkpoint_setup_lock:
        if _checkpoint_setup_succeeded_once:
            return True
        try:
            _log.warning("Checkpoint tables missing; running checkpointer.setup(): %s", exc)
            cp.setup()
            _checkpoint_setup_succeeded_once = True
        except Exception:
            _log.exception("checkpointer.setup() after missing table failed")
            return False
    return True


def _is_closed_connection_error(exc: BaseException) -> bool:
    for e in _exception_chain(exc):
        msg = str(e).lower()
        if "connection is closed" in msg:
            return True
    return False


def _rebuild_postgres_graph_after_connection_error(exc: BaseException) -> bool:
    """Re-open PostgresSaver and rebuild compiled graph when its connection has closed."""
    global _graph, _checkpointer_cm, _checkpoint_setup_succeeded_once
    if not _is_closed_connection_error(exc):
        return False
    if os.environ.get("CHECKPOINTER", "postgres").lower() == "memory":
        return False

    with _graph_lock:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver

            conn_str = _checkpoint_conn_str or checkpoint_conn_str()
            if _checkpointer_cm is not None:
                try:
                    _checkpointer_cm.__exit__(None, None, None)
                except Exception:
                    _log.warning("Ignoring error while closing stale checkpointer", exc_info=True)
            cm = PostgresSaver.from_conn_string(conn_str)
            checkpointer = cm.__enter__()
            checkpointer.setup()
            _checkpoint_setup_succeeded_once = True
            _checkpointer_cm = cm
            _graph = build_app_graph(checkpointer)
            _log.warning("Rebuilt LangGraph checkpointer after closed Postgres connection")
            return True
        except Exception:
            _log.exception("Failed to rebuild checkpointer after closed connection")
            return False


def _get_graph_state(thread_id: str) -> Any:
    assert _graph is not None
    try:
        return _graph.get_state(_make_config(thread_id))
    except Exception as e:
        if _ensure_checkpoint_tables_from_error(e):
            return _graph.get_state(_make_config(thread_id))
        if _rebuild_postgres_graph_after_connection_error(e):
            assert _graph is not None
            return _graph.get_state(_make_config(thread_id))
        raise


def _json_safe_state_values(values: Any) -> dict[str, Any]:
    """Turn checkpoint channel values into JSON-serializable dicts (e.g. LangGraph Interrupt dataclasses)."""

    def _enc(v: Any) -> Any:
        if isinstance(v, Interrupt):
            return {"id": v.id, "value": _enc(v.value)}
        if isinstance(v, (list, tuple)):
            return [_enc(x) for x in v]
        if isinstance(v, dict):
            return {str(k): _enc(x) for k, x in v.items()}
        try:
            return jsonable_encoder(v)
        except Exception:
            return str(v)

    if values is None:
        return {}
    if isinstance(values, dict):
        return {str(k): _enc(x) for k, x in values.items()}
    return {"__root__": _enc(values)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _graph, _checkpointer_cm, _checkpoint_conn_str, _checkpoint_setup_succeeded_once
    use_memory = os.environ.get("CHECKPOINTER", "postgres").lower() == "memory"
    if use_memory:
        from langgraph.checkpoint.memory import MemorySaver

        _checkpoint_conn_str = None
        saver = MemorySaver()
        _graph = build_app_graph(saver)
        _log.info(
            "LLM routing: PRIMARY_MODEL=%r SCOUT_MODEL=%r LITELLM_BASE_URL=%r",
            os.environ.get("PRIMARY_MODEL", ""),
            os.environ.get("SCOUT_MODEL", ""),
            os.environ.get("LITELLM_BASE_URL", ""),
        )
        yield
        return

    from langgraph.checkpoint.postgres import PostgresSaver

    conn_str = checkpoint_conn_str()
    _checkpoint_conn_str = conn_str

    cm = PostgresSaver.from_conn_string(conn_str)
    checkpointer = cm.__enter__()
    checkpointer.setup()
    _checkpoint_setup_succeeded_once = True
    _checkpointer_cm = cm
    _graph = build_app_graph(checkpointer)
    _log.info(
        "LLM routing: PRIMARY_MODEL=%r SCOUT_MODEL=%r LITELLM_BASE_URL=%r",
        os.environ.get("PRIMARY_MODEL", ""),
        os.environ.get("SCOUT_MODEL", ""),
        os.environ.get("LITELLM_BASE_URL", ""),
    )
    try:
        yield
    finally:
        if _checkpointer_cm is not None:
            _checkpointer_cm.__exit__(None, None, None)
            _checkpointer_cm = None


app = FastAPI(title="Agent Universe — Compute API", lifespan=lifespan)


def _run_graph_first_pass(tid: str, topic: str, project_name: str) -> None:
    assert _graph is not None
    _clear_error(tid)
    with _run_registry_lock:
        max_iters = int(
            (_run_registry.get(tid) or {}).get("max_iterations_before_approval")
            or _default_quality_max_revisions()
        )
    inp: dict[str, Any] = {
        "thread_id": tid,
        "topic": topic,
        "project_name": project_name,
        "max_iterations_before_approval": max_iters,
    }
    try:
        with _graph_lock:
            _graph.invoke(inp, _make_config(tid))
    except RuntimeError as e:
        _set_error(tid, str(e))
    except Exception:
        _set_error(tid, traceback.format_exc())


def _resume_graph(tid: str, approved: bool) -> None:
    assert _graph is not None
    _clear_error(tid)
    try:
        from langgraph.types import Command

        with _graph_lock:
            _graph.invoke(Command(resume={"approved": approved}), _make_config(tid))
    except RuntimeError as e:
        _set_error(tid, str(e))
    except Exception:
        _set_error(tid, traceback.format_exc())


def _resume_cancel(tid: str) -> None:
    assert _graph is not None
    _clear_error(tid)
    try:
        from langgraph.types import Command

        with _graph_lock:
            _graph.invoke(Command(resume={"cancelled": True}), _make_config(tid))
    except RuntimeError as e:
        _set_error(tid, str(e))
    except Exception:
        _set_error(tid, traceback.format_exc())


def _continue_graph(thread_id: str) -> None:
    """Continue a stranded run from its latest checkpoint (non-interrupt path)."""
    assert _graph is not None
    _clear_error(thread_id)
    try:
        with _graph_lock:
            # None resumes from current checkpoint; {} can be treated as fresh input and restart at entrypoint.
            _graph.invoke(None, _make_config(thread_id))
    except RuntimeError as e:
        _set_error(thread_id, str(e))
    except Exception:
        _set_error(thread_id, traceback.format_exc())


@app.post("/runs")
def start_run(body: StartRunBody) -> dict[str, Any]:
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not ready")
    tid = str(uuid.uuid4())
    _register_run(
        tid,
        body.topic,
        body.project_name,
        int(body.max_iterations_before_approval),
    )
    _start_run_worker(tid, "start", _run_graph_first_pass, tid, body.topic, body.project_name)
    return {"thread_id": tid, "status": "started"}


@app.post("/runs/{thread_id}/recover")
def recover_run(thread_id: str) -> dict[str, Any]:
    """Recover a stranded run that exists in checkpoints but not active in memory."""
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not ready")
    try:
        snap = _get_graph_state(thread_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"No checkpoint state for thread: {e!s}") from e

    next_nodes = list(snap.next) if snap.next else []
    raw_vals = getattr(snap, "values", None)
    values: dict[str, Any] = dict(raw_vals) if isinstance(raw_vals, dict) else {}
    topic = str(values.get("topic") or "").strip()
    project_name = str(values.get("project_name") or "default").strip() or "default"
    try:
        max_iters = int(values.get("max_iterations_before_approval") or _default_quality_max_revisions())
    except Exception:
        max_iters = _default_quality_max_revisions()
    max_iters = max(1, max_iters)

    if not topic and next_nodes:
        raise HTTPException(
            status_code=400,
            detail="Cannot recover this thread: missing topic in checkpoint values.",
        )

    with _run_registry_lock:
        rec = _run_registry.setdefault(thread_id, {})
        if topic:
            rec["topic"] = topic
        rec["project_name"] = project_name
        rec["max_iterations_before_approval"] = max_iters
        rec.setdefault("started_at", datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        rec.pop("cancel_requested", None)

    interrupted = bool(values.get("__interrupt__")) or ("human_approval" in next_nodes)
    if interrupted:
        return {
            "thread_id": thread_id,
            "status": "awaiting_human_approval",
            "next_nodes": next_nodes,
            "resume_hint": "Use approve/reject/cancel for interrupt resumes.",
        }
    if not next_nodes:
        return {"thread_id": thread_id, "status": "idle_or_completed", "next_nodes": []}

    started = _start_run_worker(thread_id, "recover_continue", _continue_graph, thread_id)
    return {
        "thread_id": thread_id,
        "status": "recover_queued" if started else "already_running",
        "next_nodes": next_nodes,
    }


@app.get("/runs")
def list_runs(
    task_status: str | None = Query(
        None,
        description="Filter: failed|rejected|cancelled|waiting_approval|building|running|completed",
    ),
    approval_state: str | None = Query(
        None,
        description="Filter: pending|approved|rejected|cancelled|not_reached",
    ),
    build_state: str | None = Query(None, description="Filter: none|pending|running|done|failed"),
    limit: int = Query(500, ge=1, le=2000),
) -> dict[str, Any]:
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not ready")
    try:
        degraded_warning: str | None = None
        use_memory = os.environ.get("CHECKPOINTER", "postgres").lower() == "memory"
        if use_memory:
            with _run_registry_lock:
                thread_ids = list(_run_registry.keys())
        else:
            if not _checkpoint_conn_str:
                raise HTTPException(status_code=503, detail="Checkpoint store not ready")
            _maybe_run_checkpoint_setup_before_list()
            try:
                thread_ids = list_thread_ids_from_postgres(_checkpoint_conn_str, limit=limit)
            except Exception as e:
                if _ensure_checkpoint_tables_from_error(e):
                    try:
                        thread_ids = list_thread_ids_from_postgres(_checkpoint_conn_str, limit=limit)
                    except Exception as e2:
                        _log.exception("list_thread_ids_from_postgres failed after setup")
                        degraded_warning = (
                            f"Postgres thread list failed ({e2!s}). "
                            "Showing in-memory registry runs only; fix POSTGRES_* / POSTGRES_CHECKPOINT_HOST / network."
                        )
                        with _run_registry_lock:
                            thread_ids = list(_run_registry.keys())
                else:
                    _log.exception("list_thread_ids_from_postgres failed; falling back to registry-only")
                    degraded_warning = (
                        f"Postgres thread list failed ({e!s}). "
                        "Showing in-memory registry runs only; fix POSTGRES_* / POSTGRES_CHECKPOINT_HOST / network."
                    )
                    with _run_registry_lock:
                        thread_ids = list(_run_registry.keys())
            else:
                with _run_registry_lock:
                    for tid in _run_registry:
                        if tid not in thread_ids:
                            thread_ids.append(tid)

        rows: list[dict[str, Any]] = []
        for tid in thread_ids:
            try:
                snap = _get_graph_state(tid)
            except Exception:
                _log.warning("get_state skipped for thread %s", tid, exc_info=True)
                continue
            err = _errors.get(tid)
            if _should_auto_recover(snap, err, tid):
                _log.warning("Auto-recover queued for orphaned run %s", tid)
                _start_run_worker(tid, "auto_recover", _continue_graph, tid)
            started_at = None
            with _run_registry_lock:
                meta = _run_registry.get(tid)
                if meta:
                    started_at = meta.get("started_at")
            try:
                row = summarize_run(tid, snap, err, started_at=started_at)
                worker = _worker_info(tid)
                checkpoint_age = _checkpoint_age_s(snap)
                next_nodes = row.get("next_nodes") or []
                orphan_stalled = (
                    (not worker.get("in_flight", False))
                    and bool(next_nodes)
                    and row.get("approval_state") != "pending"
                    and checkpoint_age is not None
                    and checkpoint_age >= _stall_seconds_threshold()
                )
                row["in_flight"] = bool(worker.get("in_flight", False))
                row["worker_kind"] = worker.get("worker_kind")
                row["worker_running_for_s"] = worker.get("worker_running_for_s")
                row["checkpoint_age_s"] = checkpoint_age
                row["stalled"] = bool(worker.get("stalled")) or orphan_stalled
                row["auto_recovering"] = row["worker_kind"] == "auto_recover"
                project_name = str(row.get("project_name") or "").strip()
                if project_name:
                    row["storage"] = _storage_paths(project_name, tid)
                row = jsonable_encoder(row)
            except Exception:
                _log.warning("summarize_run failed for thread %s", tid, exc_info=True)
                continue
            if passes_filters(
                row,
                task_status=task_status or None,
                approval_state=approval_state or None,
                build_state=build_state or None,
            ):
                rows.append(row)

        def _sort_key(r: dict[str, Any]) -> tuple[str, str]:
            t = r.get("started_at") or r.get("checkpoint_time") or ""
            return (str(t), r.get("thread_id", ""))

        rows.sort(key=_sort_key, reverse=True)
        out: dict[str, Any] = {"runs": rows, "count": len(rows)}
        if not use_memory and degraded_warning:
            out["warning"] = degraded_warning
            out["degraded"] = True
        return out
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("list_runs failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/runs/{thread_id}/approve")
def approve_run(thread_id: str, body: ApproveBody) -> dict[str, Any]:
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not ready")
    try:
        snap = _get_graph_state(thread_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"No checkpoint state for thread: {e!s}") from e
    next_nodes = list(snap.next) if snap.next else []
    if not _is_waiting_human_from_snapshot(snap):
        return {
            "thread_id": thread_id,
            "status": "not_waiting_human_approval",
            "approved": body.approved,
            "next_nodes": next_nodes,
        }
    started = _start_run_worker(thread_id, "approve", _resume_graph, thread_id, body.approved)
    return {
        "thread_id": thread_id,
        "status": "resume_queued" if started else "already_running",
        "approved": body.approved,
        "next_nodes": next_nodes,
    }


@app.post("/runs/{thread_id}/cancel")
def cancel_run(thread_id: str) -> dict[str, Any]:
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not ready")
    with _run_registry_lock:
        had_meta = thread_id in _run_registry
    mark_cancelled(thread_id)
    kill_aider_process(thread_id)
    with _run_registry_lock:
        _run_registry.setdefault(thread_id, {})["cancel_requested"] = True
    try:
        snap = _get_graph_state(thread_id)
    except Exception as e:
        if not had_meta:
            with _run_registry_lock:
                _run_registry.pop(thread_id, None)
            clear_cancelled(thread_id)
        raise HTTPException(
            status_code=502,
            detail=f"Checkpoint read failed: {e!s}",
        ) from e
    next_nodes = list(snap.next) if snap.next else []
    raw_vals = getattr(snap, "values", None)
    values: dict[str, Any] = dict(raw_vals) if isinstance(raw_vals, dict) else {}
    interrupted = bool(values.get("__interrupt__"))
    resume_queued = interrupted or "human_approval" in next_nodes
    resume_started = False
    if resume_queued:
        resume_started = _start_run_worker(thread_id, "cancel_resume", _resume_cancel, thread_id)
    worker = _worker_info(thread_id)
    immediate_cancel = (not resume_queued) and (not worker.get("in_flight", False))
    if immediate_cancel:
        # No active worker to consume cooperative cancel checks; mark as cancelled immediately.
        _set_error(thread_id, "Run cancelled by user")
    return {
        "thread_id": thread_id,
        "status": "cancelled" if immediate_cancel else "cancel_requested",
        "resume_queued": bool(resume_queued and resume_started),
        "in_flight": bool(worker.get("in_flight", False)),
    }


@app.post("/runs/{thread_id}/restart")
def restart_run(thread_id: str) -> dict[str, Any]:
    """Clear checkpoints for this thread_id and start the workflow again (same topic/project from registry)."""
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not ready")
    with _run_registry_lock:
        meta = _run_registry.get(thread_id)
    if not meta:
        try:
            snap = _get_graph_state(thread_id)
        except Exception:
            raise HTTPException(status_code=404, detail="Unknown thread_id (no registry entry).") from None
        raw_vals = getattr(snap, "values", None)
        values: dict[str, Any] = dict(raw_vals) if isinstance(raw_vals, dict) else {}
        topic_from_state = str(values.get("topic") or "").strip()
        if not topic_from_state:
            raise HTTPException(
                status_code=400,
                detail="Cannot restart this thread: missing topic in checkpoint values.",
            )
        project_from_state = str(values.get("project_name") or "default").strip() or "default"
        try:
            max_iters = int(values.get("max_iterations_before_approval") or _default_quality_max_revisions())
        except Exception:
            max_iters = _default_quality_max_revisions()
        with _run_registry_lock:
            meta = _run_registry.setdefault(thread_id, {})
            meta["topic"] = topic_from_state
            meta["project_name"] = project_from_state
            meta["max_iterations_before_approval"] = max(1, max_iters)
            meta.setdefault("started_at", datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    topic = str(meta.get("topic") or "")
    project_name = str(meta.get("project_name") or "default")
    if not topic.strip():
        raise HTTPException(status_code=400, detail="Registry entry has empty topic.")

    with _run_registry_lock:
        _run_registry.setdefault(thread_id, {}).pop("cancel_requested", None)

    def _restart() -> None:
        try:
            with _graph_lock:
                snap = None
                try:
                    snap = _get_graph_state(thread_id)
                except Exception:
                    snap = None
                if snap is not None and _thread_is_mid_execution(snap, thread_id):
                    _set_error(
                        thread_id,
                        "restart: run is still executing; cancel and wait until it stops.",
                    )
                    return
                cp = getattr(_graph, "checkpointer", None)
                if cp is not None and hasattr(cp, "delete_thread"):
                    cp.delete_thread(thread_id)
            clear_cancelled(thread_id)
            kill_aider_process(thread_id)
            _clear_error(thread_id)
            clear_build_buffer(thread_id)
            _run_graph_first_pass(thread_id, topic, project_name)
        except Exception:
            _set_error(thread_id, traceback.format_exc())

    started = _start_run_worker(thread_id, "restart", _restart)
    return {"thread_id": thread_id, "status": "restart_queued" if started else "already_running"}


@app.delete("/runs/{thread_id}")
def delete_run(thread_id: str) -> dict[str, Any]:
    """Remove checkpoints and registry entry for this run."""
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not ready")

    with _run_registry_lock:
        in_registry = thread_id in _run_registry

    with _graph_lock:
        snap = None
        try:
            snap = _get_graph_state(thread_id)
        except Exception:
            snap = None
        if snap is not None and _thread_is_mid_execution(snap, thread_id):
            raise HTTPException(
                status_code=409,
                detail="Run is still executing on the graph; cancel and wait until it stops.",
            )
        cp = getattr(_graph, "checkpointer", None)
        if cp is not None and hasattr(cp, "delete_thread"):
            cp.delete_thread(thread_id)

    if not in_registry and snap is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown thread_id (no registry entry and no checkpoint).",
        )

    clear_cancelled(thread_id)
    kill_aider_process(thread_id)
    _clear_error(thread_id)
    clear_build_buffer(thread_id)
    with _run_registry_lock:
        _run_registry.pop(thread_id, None)
    return {"thread_id": thread_id, "status": "deleted"}


@app.get("/runs/{thread_id}/state")
def run_state(thread_id: str) -> dict[str, Any]:
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not ready")
    try:
        snap = _get_graph_state(thread_id)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Checkpoint read failed: {e!s}",
        ) from e
    values = _json_safe_state_values(snap.values)
    err = _errors.get(thread_id)
    with _run_registry_lock:
        meta = _run_registry.get(thread_id)
    cancel_requested = bool(meta.get("cancel_requested")) if meta else False
    project_name = str(values.get("project_name") or (meta or {}).get("project_name") or "default").strip() or "default"
    worker = _worker_info(thread_id)
    next_nodes = list(snap.next) if snap.next else []
    has_interrupt = bool(values.get("__interrupt__"))
    checkpoint_age = _checkpoint_age_s(snap)
    orphan_stalled = (
        (not worker.get("in_flight", False))
        and bool(next_nodes)
        and (not has_interrupt)
        and checkpoint_age is not None
        and checkpoint_age >= _stall_seconds_threshold()
    )
    stalled = bool(worker.get("stalled")) or orphan_stalled
    return {
        "thread_id": thread_id,
        "next": next_nodes,
        "values": values,
        "error": err,
        "cancel_requested": cancel_requested,
        "storage": _storage_paths(project_name, thread_id),
        "run_activity": {
            **worker,
            "checkpoint_age_s": checkpoint_age,
            "orphan_stalled": orphan_stalled,
            "stalled": stalled,
        },
    }


@app.get("/runs/{thread_id}/logs")
def run_logs(thread_id: str) -> dict[str, Any]:
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not ready")
    try:
        snap = _get_graph_state(thread_id)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Checkpoint read failed: {e!s}",
        ) from e
    values = dict(snap.values) if snap.values else {}
    checkpoint_logs = str(values.get("build_logs") or "")
    live = get_build_buffer(thread_id)
    merged = checkpoint_logs
    if live:
        merged = f"{checkpoint_logs}{live}" if checkpoint_logs else live
    return {"thread_id": thread_id, "build_logs": merged}


def _slack_brief_state(thread_id: str) -> str:
    st = run_state(thread_id)
    vals = st.get("values") or {}
    next_nodes = st.get("next") or []
    act = st.get("run_activity") or {}
    lines = [
        f"Thread `{thread_id}`",
        f"- revisions: {vals.get('revision_count')}",
        f"- max_iterations_before_approval: {vals.get('max_iterations_before_approval')}",
        f"- quality_passed: {vals.get('quality_passed')}",
        f"- approval_status: {vals.get('approval_status') or 'not_reached'}",
        f"- next: {', '.join(str(x) for x in next_nodes) if next_nodes else '(none)'}",
        f"- in_flight: {act.get('in_flight')} ({act.get('worker_kind') or 'n/a'})",
        f"- stalled: {act.get('stalled')}",
    ]
    err = st.get("error")
    if err:
        lines.append(f"- error: {err}")
    dash = (os.environ.get("AGENT_UNIVERSE_DASHBOARD_URL") or "").strip()
    if dash:
        lines.append(f"- dashboard: {dash}")
    return "\n".join(lines)


def _slack_runs_summary() -> str:
    payload = list_runs(limit=20)
    rows = payload.get("runs") or []
    if not rows:
        return "No runs found."
    lines = ["Recent runs (max 20):"]
    for r in rows[:20]:
        lines.append(
            f"- `{r.get('thread_id')}` | task={r.get('task_status')} "
            f"approval={r.get('approval_state')} next={','.join(r.get('next_nodes') or []) or '(none)'} "
            f"iter={r.get('revision_count')}/{r.get('max_iterations_before_approval')}"
        )
    warning = payload.get("warning")
    if warning:
        lines.append(f"Warning: {warning}")
    return "\n".join(lines)


def _slack_dispatch_action(action: str, thread_id: str, *, confirm: bool = False) -> str:
    action = action.strip().lower()
    if action == "approve":
        out = approve_run(thread_id, ApproveBody(approved=True))
        return f"approve -> {out.get('status')} for `{thread_id}`"
    if action == "reject":
        out = approve_run(thread_id, ApproveBody(approved=False))
        return f"reject -> {out.get('status')} for `{thread_id}`"
    if action == "cancel":
        out = cancel_run(thread_id)
        return f"cancel -> {out.get('status')} (resume_queued={out.get('resume_queued')}) for `{thread_id}`"
    if action == "recover":
        out = recover_run(thread_id)
        return f"recover -> {out.get('status')} for `{thread_id}`"
    if action == "restart":
        out = restart_run(thread_id)
        return f"restart -> {out.get('status')} for `{thread_id}`"
    if action == "delete":
        if not confirm:
            return "delete requires --confirm"
        out = delete_run(thread_id)
        return f"delete -> {out.get('status')} for `{thread_id}`"
    if action == "state":
        return _slack_brief_state(thread_id)
    return f"Unknown action: {action}"


@app.post("/slack/commands")
async def slack_commands(request: Request) -> dict[str, str]:
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not _verify_slack_signature(headers, body):
        return _slack_ephemeral("Slack signature verification failed.")
    form = _parse_slack_form(body)
    user_id = (form.get("user_id") or "").strip()
    if not _slack_user_allowed(user_id):
        return _slack_ephemeral("You are not allowed to run Agent Universe commands.")

    text = (form.get("text") or "").strip()
    if not text:
        return _slack_ephemeral(
            "Commands: runs | state <thread_id> | approve <thread_id> | reject <thread_id> | "
            "cancel <thread_id> | recover <thread_id> | restart <thread_id> | delete <thread_id> --confirm"
        )
    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]

    try:
        if cmd == "runs":
            return _slack_ephemeral(_slack_runs_summary())
        if cmd in {"approve", "reject", "cancel", "recover", "restart", "state"}:
            if not args:
                return _slack_ephemeral(f"`{cmd}` requires <thread_id>")
            return _slack_ephemeral(_slack_dispatch_action(cmd, args[0], confirm=False))
        if cmd == "delete":
            if not args:
                return _slack_ephemeral("`delete` requires <thread_id> --confirm")
            confirm = "--confirm" in args[1:]
            return _slack_ephemeral(_slack_dispatch_action("delete", args[0], confirm=confirm))
        return _slack_ephemeral(f"Unknown command: {cmd}")
    except HTTPException as e:
        return _slack_ephemeral(f"API error {e.status_code}: {e.detail}")
    except Exception as e:
        _log.exception("slack command failed")
        return _slack_ephemeral(f"Slack command failed: {e!s}")


@app.post("/slack/interactions")
async def slack_interactions(request: Request) -> dict[str, str]:
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not _verify_slack_signature(headers, body):
        return _slack_ephemeral("Slack signature verification failed.")
    form = _parse_slack_form(body)
    payload_raw = form.get("payload") or ""
    if not payload_raw:
        return _slack_ephemeral("Missing Slack interaction payload.")
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return _slack_ephemeral("Invalid Slack interaction payload.")

    user_id = str((payload.get("user") or {}).get("id") or "")
    if not _slack_user_allowed(user_id):
        return _slack_ephemeral("You are not allowed to perform this action.")

    acts = payload.get("actions") or []
    if not acts:
        return _slack_ephemeral("No action found.")
    act = acts[0]
    action_id = str(act.get("action_id") or "").strip().lower()
    value = str(act.get("value") or "")

    try:
        meta = json.loads(value) if value else {}
    except json.JSONDecodeError:
        meta = {}
    thread_id = str(meta.get("thread_id") or "")
    action = str(meta.get("action") or action_id.replace("au_", "")).lower()
    confirm = bool(meta.get("confirm"))
    if not thread_id:
        return _slack_ephemeral("Action missing thread_id.")

    try:
        msg = _slack_dispatch_action(action, thread_id, confirm=confirm)
        return _slack_ephemeral(msg)
    except HTTPException as e:
        return _slack_ephemeral(f"API error {e.status_code}: {e.detail}")
    except Exception as e:
        _log.exception("slack interaction failed")
        return _slack_ephemeral(f"Slack interaction failed: {e!s}")


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness: process is up. Readiness for graph is the same (lifespan completes before serving)."""
    mode = os.environ.get("CHECKPOINTER", "postgres").lower()
    return {
        "status": "ok",
        "graph_ready": _graph is not None,
        "checkpointer": mode,
        "postgres_checkpoint_configured": bool(_checkpoint_conn_str),
    }
