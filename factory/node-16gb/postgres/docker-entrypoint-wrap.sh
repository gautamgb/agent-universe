#!/usr/bin/env bash
# Wraps the official postgres entrypoint: after data exists, ensure pg_hba allows the
# 32GB compute node (Tailscale IP from TAILSCALE_32GB_IP). Fresh DBs get the same rule
# from docker-entrypoint-initdb.d/01-pg_hba.sh.
set -euo pipefail

MARKER="# agent-universe: tailscale 32gb node"

patch_pg_hba() {
  local ts="${TAILSCALE_32GB_IP:-}"
  if [ -z "$ts" ]; then
    echo "postgres: TAILSCALE_32GB_IP unset — skipping pg_hba Tailscale rule (set in .env for 32GB compute access)."
    return 0
  fi

  local pgdata="${PGDATA:-/var/lib/postgresql/data}"
  local hba="${pgdata}/pg_hba.conf"
  [ -f "$hba" ] || return 0

  if grep -qF "${ts}/32" "$hba" 2>/dev/null; then
    return 0
  fi

  {
    echo ""
    echo "$MARKER"
    echo "host all all ${ts}/32 scram-sha-256"
  } >>"$hba"
}

if [ -d "${PGDATA:-/var/lib/postgresql/data}" ] && [ -f "${PGDATA:-/var/lib/postgresql/data}/PG_VERSION" ]; then
  patch_pg_hba
fi

exec /usr/local/bin/docker-entrypoint.sh "$@"
