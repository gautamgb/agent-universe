#!/usr/bin/env bash
# Runs once on first cluster init (empty volume). Adds pg_hba rule for Tailscale 32GB node (optional).
set -euo pipefail

ts="${TAILSCALE_32GB_IP:-}"
if [ -z "$ts" ]; then
  echo "postgres initdb: TAILSCALE_32GB_IP unset — skipping pg_hba Tailscale rule."
  exit 0
fi

echo "# agent-universe: tailscale 32gb node (initdb)" >>"$PGDATA/pg_hba.conf"
echo "host all all ${ts}/32 scram-sha-256" >>"$PGDATA/pg_hba.conf"
