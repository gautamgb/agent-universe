"""Cooperative cancellation for LangGraph runs (same thread_id)."""

from __future__ import annotations

import threading

_cancelled: set[str] = set()
_lock = threading.Lock()


def mark_cancelled(thread_id: str) -> None:
    with _lock:
        _cancelled.add(thread_id)


def clear_cancelled(thread_id: str) -> None:
    """Remove cancellation flag before restarting a run with the same thread_id."""
    with _lock:
        _cancelled.discard(thread_id)


def is_cancelled(thread_id: str | None) -> bool:
    if not thread_id:
        return False
    with _lock:
        return thread_id in _cancelled


def raise_if_cancelled(thread_id: str | None) -> None:
    if is_cancelled(thread_id):
        raise RuntimeError("Run cancelled by user")
