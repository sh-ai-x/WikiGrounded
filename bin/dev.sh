#!/usr/bin/env bash
# bin/dev.sh — one-shot dev launcher for the AgentOps Workbench.
#
# First run bootstraps:
#   - copies .env.example -> apps/agentops-workbench/.env (if missing)
#   - generates a 64-char JWT signing secret
#   - bakes AGENTOPS_ALLOW_DEV_TOKEN=1 + AGENTOPS_DEV_TOKEN_ANY_PROVIDER=1
#     so the API exposes /v1/auth/dev-token automatically
#   - sets provider=local-fake (safe default; flip in .env if you have a key)
#   - mints a 10-year JWT and writes it to web/.env.local
#
# Subsequent runs just start the API (:8000) and the web (:3000).
#
# Usage:
#   bin/dev.sh              # foreground — Ctrl+C stops both
#   bin/dev.sh --detach     # background — log to ./tmp/dev-{api,web}.log
#   bin/dev.sh --stop       # kill backgrounded processes
#   bin/dev.sh --status     # show what's running
#   bin/dev.sh --reset      # wipe .env and web/.env.local, re-bootstrap next run
#   bin/dev.sh --help

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$REPO_ROOT/apps/agentops-workbench"
WEB_DIR="$APP_DIR/web"
API_PORT="${AGENTOPS_APP_PORT:-8000}"
WEB_PORT=3000
LOG_DIR="$REPO_ROOT/tmp"
PID_DIR="$LOG_DIR/dev-pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

# ---------- helpers ----------

random_secret() {
  python3 -c "import secrets; print(secrets.token_urlsafe(48))" 2>/dev/null \
    || uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
}

set_or_replace_env() {
  # set_or_replace_env <file> <KEY> <value>   — preserves comments, idempotent
  local file="$1" key="$2" val="$3"
  if grep -qE "^${key}=" "$file" 2>/dev/null; then
    sed -i '' "s|^${key}=.*|${key}=${val}|" "$file"
  else
    printf "\n%s=%s\n" "$key" "$val" >> "$file"
  fi
}

bootstrap() {
  local env_file="$APP_DIR/.env"
  local web_env="$WEB_DIR/.env.local"

  # 1. .env from .env.example if missing
  if [ ! -f "$env_file" ]; then
    cp "$APP_DIR/.env.example" "$env_file"
    chmod 600 "$env_file"
    echo "[setup] created $env_file from .env.example"
  fi

  # 2. JWT secret — 64 chars, never the dev default
  if grep -qE "^AGENTOPS_JWT_SECRET=(dev-only-please-rotate|)$" "$env_file" 2>/dev/null \
     || ! grep -qE "^AGENTOPS_JWT_SECRET=" "$env_file" 2>/dev/null; then
    local secret
    secret="$(random_secret)"
    set_or_replace_env "$env_file" "AGENTOPS_JWT_SECRET" "$secret"
    set_or_replace_env "$env_file" "AGENTOPS_JWT_EXPIRY_SECONDS" "315360000"
    echo "[setup] generated JWT secret (10-year expiry)"
  fi

  # 3. dev-mint flags — always on for local dev
  set_or_replace_env "$env_file" "AGENTOPS_ALLOW_DEV_TOKEN" "true"
  set_or_replace_env "$env_file" "AGENTOPS_DEV_TOKEN_ANY_PROVIDER" "true"
  set_or_replace_env "$env_file" "AGENTOPS_PROVIDER" "local-fake"

  # 4. web bearer — start API briefly to mint a 10y token
  if [ ! -f "$web_env" ] || ! grep -qE "^NEXT_PUBLIC_AGENTOPS_DEV_BEARER=eyJ" "$web_env"; then
    echo "[setup] minting a 10-year bearer token (starting API briefly)..."
    (
      cd "$APP_DIR"
      AGENTOPS_DEV_TOKEN_ANY_PROVIDER=true AGENTOPS_ALLOW_DEV_TOKEN=true \
        uv run uvicorn agentops_workbench.api.server:app --host 127.0.0.1 --port "$API_PORT" \
        >"$LOG_DIR/dev-bootstrap-api.log" 2>&1 &
      local api_pid=$!
      echo $api_pid > "$PID_DIR/bootstrap-api.pid"
      for _ in 1 2 3 4 5 6 8 10 15 20; do
        if curl -fsS -o /dev/null -m 1 "http://127.0.0.1:$API_PORT/v1/auth/dev-mode" 2>/dev/null; then
          break
        fi
        sleep 1
      done
      local tok
      tok="$(curl -fsS -m 3 "http://127.0.0.1:$API_PORT/v1/auth/dev-token?principal_id=dev-user" \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])" 2>/dev/null || true)"
      if [ -z "${tok:-}" ]; then
        echo "[setup] ERROR: API didn't mint a token. Check $LOG_DIR/dev-bootstrap-api.log"
        kill "$api_pid" 2>/dev/null || true
        exit 1
      fi
      printf "NEXT_PUBLIC_AGENTOPS_DEV_BEARER=%s\n" "$tok" > "$web_env"
      chmod 600 "$web_env"
      kill "$api_pid" 2>/dev/null || true
      rm -f "$PID_DIR/bootstrap-api.pid"
      echo "[setup] baked bearer into $web_env"
    )
  fi
  echo "[setup] ready."
}

start_bg() {
  local api_cmd web_cmd api_pid web_pid
  api_cmd="AGENTOPS_DEV_TOKEN_ANY_PROVIDER=true AGENTOPS_ALLOW_DEV_TOKEN=true uv run uvicorn agentops_workbench.api.server:app --host 127.0.0.1 --port $API_PORT"
  web_cmd="npm run dev"
  ( cd "$APP_DIR" && nohup bash -c "$api_cmd" >"$LOG_DIR/dev-api.log" 2>&1 & echo $! > "$PID_DIR/api.pid" )
  ( cd "$WEB_DIR" && nohup bash -c "$web_cmd"   >"$LOG_DIR/dev-web.log" 2>&1 & echo $! > "$PID_DIR/web.pid" )
  for _ in 1 2 3 4 5 8 10 15 20; do
    api=$(curl -sS -o /dev/null -w "%{http_code}" -m 1 "http://127.0.0.1:$API_PORT/v1/auth/dev-mode" 2>/dev/null || echo 000)
    web=$(curl -sS -o /dev/null -w "%{http_code}" -m 1 "http://127.0.0.1:$WEB_PORT" 2>/dev/null || echo 000)
    [ "$api" = "200" ] && [ "$web" = "200" ] && break
    sleep 1
  done
  echo "API  -> http://127.0.0.1:$API_PORT  (pid $(cat "$PID_DIR/api.pid"), log: $LOG_DIR/dev-api.log)"
  echo "Web  -> http://localhost:$WEB_PORT  (pid $(cat "$PID_DIR/web.pid"), log: $LOG_DIR/dev-web.log)"
  echo "Open http://localhost:$WEB_PORT"
}

start_fg() {
  local api_pid web_pid
  ( cd "$APP_DIR" && \
    AGENTOPS_DEV_TOKEN_ANY_PROVIDER=true AGENTOPS_ALLOW_DEV_TOKEN=true \
    uv run uvicorn agentops_workbench.api.server:app --host 127.0.0.1 --port "$API_PORT" ) &
  api_pid=$!
  ( cd "$WEB_DIR" && npm run dev ) &
  web_pid=$!
  trap 'kill $api_pid $web_pid 2>/dev/null; wait 2>/dev/null' INT TERM EXIT
  echo "API  -> http://127.0.0.1:$API_PORT  (pid $api_pid)"
  echo "Web  -> http://localhost:$WEB_PORT  (pid $web_pid)"
  echo "Open http://localhost:$WEB_PORT. Ctrl+C to stop both."
  wait
}

stop_bg() {
  for pf in "$PID_DIR"/*.pid; do
    [ -f "$pf" ] || continue
    local pid name
    pid=$(cat "$pf"); name=$(basename "$pf" .pid)
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" && echo "[$name] stopped (pid $pid)"
    fi
    rm -f "$pf"
  done
  for port in "$API_PORT" "$WEB_PORT"; do
    pids=$(lsof -ti:"$port" 2>/dev/null || true)
    [ -n "$pids" ] && kill $pids 2>/dev/null && echo "killed stragglers on :$port"
  done
}

status() {
  for port in "$API_PORT" "$WEB_PORT"; do
    code=$(curl -sS -o /dev/null -w "%{http_code}" -m 1 "http://127.0.0.1:$port" 2>/dev/null || echo 000)
    pid=$(lsof -ti:"$port" 2>/dev/null | head -1 || echo "-")
    printf "  :%-5d  pid=%-6s  http=%s\n" "$port" "$pid" "$code"
  done
}

reset_all() {
  stop_bg
  rm -f "$APP_DIR/.env" "$WEB_DIR/.env.local" \
        "$LOG_DIR/dev-bootstrap-api.log"
  echo "wiped .env and web/.env.local. Run bin/dev.sh to re-bootstrap."
}

case "${1:-fg}" in
  ""|fg|foreground) bootstrap; start_fg ;;
  --detach|--bg)     bootstrap; start_bg ;;
  --stop)            stop_bg ;;
  --status)          status ;;
  --reset)           reset_all ;;
  --help|-h)         sed -n '2,22p' "$0" ;;
  *) echo "unknown arg: $1 (try --help)"; exit 1 ;;
esac
