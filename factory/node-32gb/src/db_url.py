"""Build DATABASE_URL from repo root .env-style variables when unset."""

from __future__ import annotations

import os
from urllib.parse import quote_plus


def checkpoint_conn_str() -> str:
    """DSN for LangGraph PostgresSaver — must match `api` lifespan (port 5433, sslmode=disable).

    Prefer DATABASE_URL when set. Otherwise POSTGRES_* with host from
    POSTGRES_CHECKPOINT_HOST or TAILSCALE_16GB_IP (same logic as src.api lifespan).
    """
    direct = (os.environ.get("DATABASE_URL") or "").strip()
    if direct:
        return direct
    user = os.getenv("POSTGRES_USER")
    raw_password = os.getenv("POSTGRES_PASSWORD")
    host = (os.getenv("POSTGRES_CHECKPOINT_HOST") or os.getenv("TAILSCALE_16GB_IP") or "").strip()
    db = os.getenv("POSTGRES_DB")
    if not (user and host and db and raw_password is not None):
        raise RuntimeError(
            "Set POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, and either "
            "POSTGRES_CHECKPOINT_HOST or TAILSCALE_16GB_IP (or DATABASE_URL) for checkpointing."
        )
    password = quote_plus(raw_password)
    return f"postgresql://{user}:{password}@{host}:5433/{db}?sslmode=disable"


def database_url() -> str:
    direct = (os.environ.get("DATABASE_URL") or "").strip()
    if direct:
        return direct
    user = os.environ.get("POSTGRES_USER", "")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("TAILSCALE_16GB_IP", "")
    db = os.environ.get("POSTGRES_DB", "")
    if not (user and password and host and db):
        raise RuntimeError(
            "Set DATABASE_URL or POSTGRES_*, TAILSCALE_16GB_IP, POSTGRES_DB for checkpointing."
        )
    safe = quote_plus(password)
    port = os.environ.get("POSTGRES_PORT", "5433")
    ssl = os.environ.get("POSTGRES_SSLMODE", "disable")
    return f"postgresql://{user}:{safe}@{host}:{port}/{db}?sslmode={ssl}"
