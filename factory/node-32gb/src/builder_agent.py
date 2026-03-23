"""Run Aider in Docker via subprocess (no shell scripts)."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Optional

# In-memory tail for UI polling while a long build runs (also merged into graph state).
_build_buffers: dict[str, str] = {}
_buffer_lock = threading.Lock()

# Track nested docker processes so cancel can terminate a long build.
_aider_procs: dict[str, subprocess.Popen] = {}
_aider_proc_lock = threading.Lock()


def get_build_buffer(thread_id: str) -> str:
    with _buffer_lock:
        return _build_buffers.get(thread_id, "")


def _append_buffer(thread_id: str, chunk: str) -> None:
    with _buffer_lock:
        _build_buffers[thread_id] = _build_buffers.get(thread_id, "") + chunk


def clear_build_buffer(thread_id: str) -> None:
    with _buffer_lock:
        _build_buffers.pop(thread_id, None)


def kill_aider_process(thread_id: str) -> None:
    """Terminate the Aider docker child process if still running (cooperative cancel during build)."""
    with _aider_proc_lock:
        proc = _aider_procs.pop(thread_id, None)
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        proc.kill()
        proc.wait(timeout=15)
    except Exception:
        pass


def _repo_root() -> Path:
    return Path(os.environ.get("AGENT_UNIVERSE_ROOT", os.getcwd())).resolve()


def _project_dir_for_docker_bind() -> Path:
    """Host path for `docker run -v …:/workspace`.

    The API container sees repo at AGENT_UNIVERSE_ROOT (/workspace), but `docker run -v`
    paths are resolved on the Docker *host*. Without AGENT_UNIVERSE_HOST_PATH, bind mounts
    can point at wrong/readonly paths → git init / aider history permission errors.
    """
    host = (os.environ.get("AGENT_UNIVERSE_HOST_PATH") or "").strip()
    if host:
        return Path(host).resolve()
    return _repo_root()


def _ollama_base_for_aider() -> str:
    """Ollama URL for the nested Aider container (must reach Ollama from sibling containers)."""
    explicit = (os.environ.get("AIDER_OLLAMA_API_BASE") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    ts = (os.environ.get("TAILSCALE_32GB_IP") or "").strip()
    if ts:
        return f"http://{ts}:11434"
    return "http://host.docker.internal:11434"




def _normalize_aider_model(model: str) -> str:
    """Map stale/legacy model IDs to available local coder aliases."""
    m = (model or "").strip()
    if not m:
        return "ollama/pl-coder:latest"
    stale = {
        "qwen2.5-coder",
        "ollama/qwen2.5-coder",
        "qwen2.5-coder:7b",
        "ollama/qwen2.5-coder:7b",
    }
    if m in stale:
        return "ollama/pl-coder:latest"
    return m

def run_aider(
    *,
    thread_id: str,
    project_name: str,
    task_prompt: str,
    github_token: Optional[str] = None,
) -> tuple[str, int]:
    """
    Run Aider in Docker (image from AIDER_DOCKER_IMAGE, default paulgauthier/aider-full) with hardened flags, streaming stdout into buffer + returned log.
    Mounts projects/<project_name> to /workspace and the deploy key read-only.
    """
    root = _project_dir_for_docker_bind()
    project_dir = root / "projects" / project_name
    project_dir.mkdir(parents=True, exist_ok=True)
    clear_build_buffer(thread_id)
    if not (os.environ.get("AGENT_UNIVERSE_HOST_PATH") or "").strip():
        _append_buffer(
            thread_id,
            "[builder] WARNING: set AGENT_UNIVERSE_HOST_PATH in .env to this repo’s absolute path on the Docker host "
            "(the machine running Docker). Otherwise `docker run -v` bind mounts can break (git/aider permission errors).\n",
        )

    key_host = os.environ.get("AIDER_SSH_KEY_CONTAINER_PATH", "").strip()
    if not key_host:
        key_host = os.path.expanduser("~/.ssh/ai_builder_ed25519")
    key_path = Path(key_host)
    if not key_path.is_file():
        msg = f"[builder] SSH key not found at {key_path}\n"
        _append_buffer(thread_id, msg)
        return msg, 1

    ollama_base = _ollama_base_for_aider()
    env_pairs = [
        "-e",
        f"OLLAMA_API_BASE={ollama_base}",
        "-w",
        "/workspace",
    ]
    if github_token:
        env_pairs.extend(["-e", f"GITHUB_TOKEN={github_token}"])

    image = (os.environ.get("AIDER_DOCKER_IMAGE") or "paulgauthier/aider-full").strip()
    raw_model = (os.environ.get("AIDER_MODEL") or "").strip()
    aider_model = _normalize_aider_model(raw_model)
    if raw_model and raw_model != aider_model:
        _append_buffer(
            thread_id,
            f"[builder] Remapped AIDER_MODEL {raw_model!r} -> {aider_model!r} to match local Ollama tags\n",
        )
    cmd: list[str] = [
        "docker",
        "run",
        "--rm",
        "--security-opt",
        "no-new-privileges",
        "--cap-drop=ALL",
        "--memory",
        "4g",
        "--cpus",
        "2",
        "-v",
        f"{project_dir}:/workspace",
        "-v",
        f"{key_path}:/root/.ssh/id_ed25519:ro",
        *env_pairs,
        image,
        "--model",
        aider_model,
        "--message",
        task_prompt,
        "--yes-always",
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    with _aider_proc_lock:
        _aider_procs[thread_id] = proc
    lines: list[str] = []
    rc = -1
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                lines.append(line)
                _append_buffer(thread_id, line)
        rc = proc.wait()
    finally:
        with _aider_proc_lock:
            _aider_procs.pop(thread_id, None)
    footer = f"\n[builder] docker exit code: {rc}\n"
    _append_buffer(thread_id, footer)
    lines.append(footer)
    return "".join(lines), rc
