#!/usr/bin/env bash
# Sync gateway (this directory) and the repository root .env to the 16GB node over Tailscale/SSH.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$DIR/../.." && pwd)"

if [ ! -f "$REPO_ROOT/.env" ]; then
  echo "ERROR: Missing $REPO_ROOT/.env (deploy requires the repo-root .env)." >&2
  exit 1
fi

if [ -f "$REPO_ROOT/.env" ]; then
  # shellcheck source=/dev/null
  set -a
  source "$REPO_ROOT/.env"
  set +a
fi

# Default path: NODE_16GB_SSH_USER @ TAILSCALE_16GB_IP, under the remote user's home (no sudo).
# NODE_16GB_REMOTE_ROOT is a single path segment relative to ~ (default: agent-universe → ~/agent-universe/...).
# Or set NODE_16GB_RSYNC_DEST to a full user@host:path (overrides the build below).
if [ -z "${NODE_16GB_RSYNC_DEST:-}" ] && [ -n "${TAILSCALE_16GB_IP:-}" ]; then
  if [ -z "${NODE_16GB_SSH_USER:-}" ]; then
    echo "ERROR: Set NODE_16GB_SSH_USER in .env to the macOS login on the 16GB Mini (run 'whoami' in Terminal on that Mac), or set NODE_16GB_RSYNC_DEST=user@host:path." >&2
    echo "Tailscale SSH rejects usernames that do not exist on the remote machine." >&2
    exit 1
  fi
  _rr="${NODE_16GB_REMOTE_ROOT:-agent-universe}"
  NODE_16GB_RSYNC_DEST="${NODE_16GB_SSH_USER}@${TAILSCALE_16GB_IP}:${_rr}/factory/node-16gb"
fi

: "${NODE_16GB_RSYNC_DEST:?Set NODE_16GB_RSYNC_DEST or NODE_16GB_SSH_USER + TAILSCALE_16GB_IP in .env.}"

RSYNC_HOST="${NODE_16GB_RSYNC_DEST%%:*}"
RSYNC_PATH="${NODE_16GB_RSYNC_DEST#*:}"
# Strip leading colon if host had no path (unlikely)
RSYNC_PATH="${RSYNC_PATH#:}"
# Path is either home-relative (agent-universe/...) or absolute (/opt/... — may need sudo on remote)
REMOTE_REPO_ROOT="$(dirname "$(dirname "$RSYNC_PATH")")"

EXCLUDES=(
  --exclude='.DS_Store'
  --exclude='postgres_data'
)

# macOS rsync is too old for --mkpath; create parents on the remote first (under ~ if path is relative).
SSH_OPTS=(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
if [[ "${RSYNC_PATH}" == /* ]]; then
  echo "NOTE: Remote path is absolute (${RSYNC_PATH}). If mkdir fails, create it on the 16GB Mac with sudo or pick NODE_16GB_REMOTE_ROOT (home-relative) instead." >&2
fi
MKDIR_CMD=$(printf 'mkdir -p %q %q' "${RSYNC_PATH}" "${REMOTE_REPO_ROOT}")
"${SSH_OPTS[@]}" "${RSYNC_HOST}" "${MKDIR_CMD}"

rsync -avz --human-readable --delete \
  -e "${SSH_OPTS[*]}" \
  "${EXCLUDES[@]}" \
  "$DIR/" \
  "${NODE_16GB_RSYNC_DEST%/}/"

rsync -avz --human-readable \
  -e "${SSH_OPTS[*]}" \
  "$REPO_ROOT/.env" \
  "${RSYNC_HOST}:${REMOTE_REPO_ROOT}/.env"

# Docker Compose reads .env next to docker-compose.yml for ${VAR} substitution — not ../../.env
rsync -avz --human-readable \
  -e "${SSH_OPTS[*]}" \
  "$REPO_ROOT/.env" \
  "${RSYNC_HOST}:${RSYNC_PATH}/.env"

VERIFY_ENV_CMD=$(printf 'test -s %q && test -s %q' "${REMOTE_REPO_ROOT}/.env" "${RSYNC_PATH}/.env")
"${SSH_OPTS[@]}" "${RSYNC_HOST}" "${VERIFY_ENV_CMD}"

echo "Synced factory/node-16gb -> ${NODE_16GB_RSYNC_DEST}"
echo "Verified remote env files: ${REMOTE_REPO_ROOT}/.env and ${RSYNC_PATH}/.env"
