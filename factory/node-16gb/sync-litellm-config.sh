#!/usr/bin/env bash
# Copy litellm_config.yaml to the 16GB gateway and restart the LiteLLM container.
#
# Requires repo root .env with at least TAILSCALE_16GB_IP (and optionally NODE_16GB_SSH_USER).
# Override remote directory if your gateway layout differs:
#   REMOTE_NODE16GB_DIR=/opt/agent-universe/factory/node-16gb ./sync-litellm-config.sh
#
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$DIR/../.." && pwd)"

if [ -f "$REPO_ROOT/.env" ]; then
  # shellcheck source=/dev/null
  set -a
  source "$REPO_ROOT/.env"
  set +a
fi

: "${TAILSCALE_16GB_IP:?Set TAILSCALE_16GB_IP in $REPO_ROOT/.env}"

REMOTE_USER="${NODE_16GB_SSH_USER:-mini16}"
# Remote factory/node-16gb directory (contains litellm_config.yaml + docker-compose.yml)
REMOTE_NODE16GB_DIR="${REMOTE_NODE16GB_DIR:-/Users/mini16/agent-universe/factory/node-16gb}"

LOCAL_FILE="$DIR/litellm_config.yaml"
REMOTE_FILE="${REMOTE_NODE16GB_DIR}/litellm_config.yaml"

SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)

echo "scp -> ${REMOTE_USER}@${TAILSCALE_16GB_IP}:${REMOTE_FILE}"
scp "${SSH_OPTS[@]}" \
  "$LOCAL_FILE" \
  "${REMOTE_USER}@${TAILSCALE_16GB_IP}:${REMOTE_FILE}"

echo "Restarting litellm on gateway..."
ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${TAILSCALE_16GB_IP}" \
  "cd '${REMOTE_NODE16GB_DIR}' && (docker compose --env-file ../../.env restart litellm || docker restart agent-universe-gateway-litellm-1)"

echo "Done."
