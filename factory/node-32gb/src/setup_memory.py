"""One-off: create LangGraph checkpoint tables in Postgres (same DSN as src.api).

Run from repo with workspace .env loaded, e.g.:
  docker compose exec api python -m src.setup_memory
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from src.db_url import checkpoint_conn_str


def _load_env() -> None:
    root = Path(os.environ.get("AGENT_UNIVERSE_ROOT", os.getcwd()))
    if root.is_dir():
        load_dotenv(root / ".env")
    load_dotenv()


def setup_db() -> None:
    _load_env()
    db_uri = checkpoint_conn_str()
    print(f"Connecting for LangGraph checkpoint setup (host from POSTGRES_CHECKPOINT_HOST or TAILSCALE_16GB_IP)...")

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg import connect
    from psycopg.rows import dict_row

    try:
        with connect(
            db_uri,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        ) as conn:
            checkpointer = PostgresSaver(conn)
            checkpointer.setup()
        print("Success: LangGraph checkpoint tables are present (checkpointer.setup()).")
    except Exception as e:
        print(f"Failed: {e}")
        raise


if __name__ == "__main__":
    setup_db()
