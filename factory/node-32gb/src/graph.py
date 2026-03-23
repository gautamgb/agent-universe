"""LangGraph workflow: scout → PM → architect ↔ quality → human approval → builder."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from src.builder_agent import run_aider
from src.run_control import raise_if_cancelled
from src.slack_notifier import notify_human_approval
from src.state import AgentState

_log = logging.getLogger(__name__)


def _workspace_env_path() -> Path:
    return Path(os.environ.get("AGENT_UNIVERSE_ROOT", os.getcwd())).resolve() / ".env"


def _dotenv_key(key: str) -> str | None:
    """Read one key from mounted repo .env (source of truth; Docker env may inject stale ollama/...:7b)."""
    path = _workspace_env_path()
    if not path.is_file():
        return None
    val = dotenv_values(path).get(key)
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _resolve_primary_model() -> str:
    """PM / architect / quality gate — matches a model_name in gateway litellm_config.yaml."""
    from_file = _dotenv_key("PRIMARY_MODEL")
    if from_file:
        return from_file
    model = (
        (os.environ.get("PRIMARY_MODEL") or "").strip()
        or (os.environ.get("LITELLM_MODEL") or "").strip()
        or "gemini-executive"
    )
    return _normalize_model_id(model, role="primary")


def _resolve_scout_model() -> str:
    """Scout node — defaults to PRIMARY_MODEL if unset."""
    from_file = _dotenv_key("SCOUT_MODEL")
    if from_file:
        return from_file
    s = (os.environ.get("SCOUT_MODEL") or "").strip()
    return _normalize_model_id(s or _resolve_primary_model(), role="scout")


def _normalize_model_id(model_id: str, *, role: str) -> str:
    """Guard against stale env values that point to missing local tags."""
    mid = (model_id or "").strip()
    if mid in {"ollama/qwen2.5-coder:7b", "qwen2.5-coder:7b"}:
        # Force stable alias routing from gateway config when stale 7b leaks in.
        fallback = "local-scout" if role == "scout" else "local-executive"
        _log.warning("Replacing stale model %r with %r for %s", mid, fallback, role)
        return fallback
    return mid


def _chat(model_id: str) -> ChatOpenAI:
    base = os.environ.get("LITELLM_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise RuntimeError(
            "Set LITELLM_BASE_URL in the master .env (e.g. http://<16GB Tailscale IP>:4040)."
        )
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    key = os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY", "")
    temperature = float(os.environ.get("LITELLM_TEMPERATURE", "0.2"))
    timeout_s = float(os.environ.get("LITELLM_REQUEST_TIMEOUT_SECONDS", "120"))
    return ChatOpenAI(
        model=model_id.strip(),
        temperature=temperature,
        api_key=key,
        base_url=base,
        timeout=timeout_s,
        max_retries=1,
    )


def _persist_run_artifact(thread_id: str, project_name: str, filename: str, content: str) -> None:
    """Write PM/architecture artifacts to disk for visibility and recovery diagnostics."""
    if not thread_id:
        return
    proj = (project_name or "default").strip() or "default"
    root = Path(os.environ.get("AGENT_UNIVERSE_ROOT", os.getcwd())).resolve()
    out_dir = root / "projects" / proj / ".agent-universe" / thread_id
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / filename).write_text(content or "", encoding="utf-8")
    except Exception:
        _log.warning("artifact write failed thread=%s file=%s", thread_id, filename, exc_info=True)


def scout_researcher(state: AgentState) -> dict[str, Any]:
    raise_if_cancelled(state.get("thread_id"))
    topic = state.get("topic") or "general system design"
    scout_model = _resolve_scout_model()
    _log.info("scout_researcher model=%r", scout_model)
    llm = _chat(scout_model)
    out = llm.invoke(
        [
            SystemMessage(
                content="You are a research scout. Produce concise bullet research useful for product and architecture."
            ),
            HumanMessage(content=f"Research topic:\n{topic}"),
        ]
    )
    text = getattr(out, "content", str(out)) or ""
    _persist_run_artifact(state.get("thread_id") or "", state.get("project_name") or "default", "research.md", text)
    return {"research": text}


def pm_synthesizer(state: AgentState) -> dict[str, Any]:
    raise_if_cancelled(state.get("thread_id"))
    llm = _chat(_resolve_primary_model())
    out = llm.invoke(
        [
            SystemMessage(
                content="You are a technical PM. Turn research into a crisp PRD-style spec: goals, users, scope, milestones, risks."
            ),
            HumanMessage(
                content=f"Research:\n{state.get('research', '')}\n\nProduce the PM spec."
            ),
        ]
    )
    text = getattr(out, "content", str(out)) or ""
    _persist_run_artifact(state.get("thread_id") or "", state.get("project_name") or "default", "pm_spec.md", text)
    return {"pm_spec": text}


def system_architect(state: AgentState) -> dict[str, Any]:
    raise_if_cancelled(state.get("thread_id"))
    llm = _chat(_resolve_primary_model())
    feedback = (state.get("quality_feedback") or "").strip()
    revision = int(state.get("revision_count") or 0)
    extra = ""
    if feedback:
        extra = f"\n\nPrior review feedback (revision {revision}):\n{feedback}\n"
    out = llm.invoke(
        [
            SystemMessage(
                content="You are a system architect. Produce a concrete architecture: components, interfaces, data flows, security, deployment. Be specific."
            ),
            HumanMessage(
                content=f"PM spec:\n{state.get('pm_spec', '')}{extra}\n\nProduce the architecture document."
            ),
        ]
    )
    text = getattr(out, "content", str(out)) or ""
    _persist_run_artifact(
        state.get("thread_id") or "",
        state.get("project_name") or "default",
        "architecture_spec.md",
        text,
    )
    return {"architecture_spec": text, "revision_count": revision + 1}


_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def _parse_quality_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK.search(text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return {"pass": False, "feedback": text or "Unparseable judge output."}


def quality_gate(state: AgentState) -> dict[str, Any]:
    raise_if_cancelled(state.get("thread_id"))
    llm = _chat(_resolve_primary_model())
    arch = state.get("architecture_spec", "")
    out = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are a strict specification reviewer. "
                    "Reply ONLY with compact JSON: "
                    '{"pass": true|false, "feedback": "why or what to fix"}. '
                    "Pass only if the architecture is implementable, coherent, and security-aware."
                )
            ),
            HumanMessage(content=f"Architecture to evaluate:\n{arch}"),
        ]
    )
    raw = getattr(out, "content", str(out)) or ""
    data = _parse_quality_json(raw)
    passed = bool(data.get("pass"))
    feedback = str(data.get("feedback") or "").strip() or raw
    _persist_run_artifact(
        state.get("thread_id") or "",
        state.get("project_name") or "default",
        "quality_feedback.md",
        feedback,
    )
    return {"quality_passed": passed, "quality_feedback": feedback}


def _route_after_quality(state: AgentState) -> Literal["system_architect", "human_approval"]:
    if state.get("quality_passed"):
        return "human_approval"
    revisions = int(state.get("revision_count") or 0)
    per_task_cap = state.get("max_iterations_before_approval")
    cap = int(per_task_cap) if per_task_cap is not None else int(os.environ.get("QUALITY_MAX_REVISIONS", "5"))
    if revisions >= cap:
        return "human_approval"
    return "system_architect"


def human_approval(state: AgentState) -> dict[str, Any]:
    # Do not raise_if_cancelled here: cancel while waiting uses Command(resume={"cancelled": True}).
    thread_id = state.get("thread_id") or ""
    topic = state.get("topic") or ""
    project = state.get("project_name") or "default"
    arch = state.get("architecture_spec", "")
    notify_human_approval(
        thread_id=thread_id,
        topic=topic,
        project_name=project,
        architecture_excerpt=arch,
    )
    decision = interrupt(
        {
            "stage": "human_approval",
            "thread_id": thread_id,
            "message": "Approve to run the builder, or reject.",
        }
    )
    approved = False
    cancelled = False
    if isinstance(decision, dict):
        if decision.get("cancelled"):
            cancelled = True
        else:
            approved = bool(decision.get("approved"))
    elif isinstance(decision, bool):
        approved = decision
    if cancelled:
        status = "cancelled"
    else:
        status = "approved" if approved else "rejected"
    return {"approval_status": status}


def _route_after_human(state: AgentState) -> Literal["builder_agent", END]:
    st = (state.get("approval_status") or "").lower()
    if st == "approved":
        return "builder_agent"
    return END


def builder_agent(state: AgentState) -> dict[str, Any]:
    raise_if_cancelled(state.get("thread_id"))
    thread_id = state.get("thread_id") or ""
    project = state.get("project_name") or "default"
    topic = state.get("topic") or ""
    arch = state.get("architecture_spec", "")
    token = os.environ.get("SCOPED_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    prompt = (
        f"Implement or adjust the project to align with this architecture.\n"
        f"Topic: {topic}\n\nArchitecture:\n{arch}\n"
    )
    logs, exit_code = run_aider(
        thread_id=thread_id,
        project_name=project,
        task_prompt=prompt,
        github_token=token,
    )
    if exit_code != 0:
        raise RuntimeError(f"Builder failed with exit code {exit_code}")
    # If cancel was requested while builder was running, surface cancelled outcome.
    raise_if_cancelled(thread_id)
    return {"build_logs": logs}


def build_app_graph(checkpointer: Any) -> Any:
    g = StateGraph(AgentState)
    g.add_node("scout_researcher", scout_researcher)
    g.add_node("pm_synthesizer", pm_synthesizer)
    g.add_node("system_architect", system_architect)
    g.add_node("quality_gate", quality_gate)
    g.add_node("human_approval", human_approval)
    g.add_node("builder_agent", builder_agent)

    g.set_entry_point("scout_researcher")
    g.add_edge("scout_researcher", "pm_synthesizer")
    g.add_edge("pm_synthesizer", "system_architect")
    g.add_edge("system_architect", "quality_gate")
    g.add_conditional_edges("quality_gate", _route_after_quality)
    g.add_conditional_edges("human_approval", _route_after_human)
    g.add_edge("builder_agent", END)

    return g.compile(checkpointer=checkpointer)
