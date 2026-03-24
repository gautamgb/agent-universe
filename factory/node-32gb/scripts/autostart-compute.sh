#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="/Users/bharti/agent-universe"
COMPOSE_FILE="$REPO_ROOT/factory/node-32gb/docker-compose.yml"
LOG_FILE="$HOME/Library/Logs/agent-universe-autostart.log"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export DOCKER_HOST="unix:///Users/bharti/.orbstack/run/docker.sock"

mkdir -p "$(dirname "$LOG_FILE")"
{
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] autostart begin"

  # Start OrbStack if not already running.
  if ! pgrep -f "OrbStack.app/Contents/MacOS/OrbStack" >/dev/null 2>&1; then
    open -a OrbStack || true
  fi

  # Wait for Docker daemon from OrbStack.
  for i in $(seq 1 60); do
    if docker --context orbstack info >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done

  if ! docker --context orbstack info >/dev/null 2>&1; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] docker daemon unavailable after wait"
    exit 1
  fi

  # Ensure compute services are up after restart/login.
  docker --context orbstack compose -f "$COMPOSE_FILE" up -d api streamlit

  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] autostart success"
} >>"$LOG_FILE" 2>&1
