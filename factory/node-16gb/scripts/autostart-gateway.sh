#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../docker-compose.yml"
LOG_FILE="$HOME/Library/Logs/agent-universe-gateway-autostart.log"

mkdir -p "$(dirname "$LOG_FILE")"

{
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] gateway autostart begin"

  # Prefer OrbStack if installed; otherwise try Docker Desktop.
  if [ -d "/Applications/OrbStack.app" ]; then
    if ! pgrep -f "OrbStack.app/Contents/MacOS/OrbStack" >/dev/null 2>&1; then
      open -a OrbStack || true
    fi
  elif [ -d "/Applications/Docker.app" ]; then
    open -a Docker || true
  fi

  # Launchd does not inherit shell PATH/context; set both explicitly.
  export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  if [ -S "$HOME/.orbstack/run/docker.sock" ]; then
    export DOCKER_HOST="unix://$HOME/.orbstack/run/docker.sock"
  fi

  for i in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done

  if ! docker info >/dev/null 2>&1; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] docker daemon unavailable after wait"
    exit 1
  fi

  # Keep gateway stack up on reboot/login.
  docker compose -f "$COMPOSE_FILE" up -d postgres redis litellm

  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] gateway autostart success"
} >>"$LOG_FILE" 2>&1
