#!/usr/bin/env bash
# demo.sh: orchestrate the Brutor Demo System (DESIGN.md section 5.6).
#
#   ./demo.sh up          build + start the MCP servers and the fraud agent,
#                         wait for their /health, run setup.py, start the
#                         screening agent
#   ./demo.sh provision   run setup.py only
#   ./demo.sh start       start (or restart) the screening agent
#   ./demo.sh status      curl the four /health endpoints + verify.py --brief
#   ./demo.sh logs [svc]  follow container logs
#   ./demo.sh down        stop the containers (keeps volumes; --volumes removes them)
#   ./demo.sh run-one [APP-id]
#                         process exactly one pending application on the running
#                         agent (default: the first id from applications_list_pending)
#   ./demo.sh generate N  add N synthetic applications to the origination mock
#
# Any extra arguments after `up` / `provision` are passed to setup.py
# (for example: ./demo.sh up --residency or --no-portal-user).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

COMPOSE_FILE="$HERE/docker-compose.yml"
COMPOSE=(docker compose -f "$COMPOSE_FILE")
PYTHON="${PYTHON:-python3}"
NETWORK="${BRUTOR_NETWORK:-brutor-network}"

APPLICATIONS_URL="${APPLICATIONS_HOST_URL:-http://127.0.0.1:3014}"
BUREAU_URL="${BUREAU_HOST_URL:-http://127.0.0.1:3015}"
FRAUD_URL="${FRAUD_HOST_URL:-http://127.0.0.1:9200}"
SCREENING_URL="${SCREENING_HOST_URL:-http://127.0.0.1:9201}"

log()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

need_env() {
  if [[ ! -f "$HERE/.env" ]]; then
    warn ".env not found; copying .env.example. Set OPENAI_API_KEY before provisioning."
    cp "$HERE/.env.example" "$HERE/.env"
  fi
  # Export .env so compose interpolation and setup.py both see it. A value that
  # is already exported in the shell wins over the file, so an operator can keep
  # OPENAI_API_KEY out of the file entirely (the template ships it empty, and a
  # plain `source` would blank the exported key). .demo.env is exported as well,
  # file wins there: the fraud service maps BRUTOR_API_KEY from
  # ${FRAUD_BRUTOR_API_KEY}, and compose interpolates from the shell, not from
  # env_file entries.
  load_env_file "$HERE/.env" keep
  if [[ -s "$HERE/.demo.env" ]]; then
    load_env_file "$HERE/.demo.env" override
  fi
}

load_env_file() {
  # $1 = file, $2 = keep (existing non-empty shell value wins) | override
  local file="$1" mode="$2" line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"; val="${line#*=}"
    key="${key//[[:space:]]/}"
    val="${val%\"}"; val="${val#\"}"
    if [[ "$mode" == "keep" && -n "${!key:-}" ]]; then continue; fi
    export "$key=$val"
  done < "$file"
}

need_network() {
  if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
    die "docker network '$NETWORK' does not exist. Start the trial bundle first (its compose creates it)."
  fi
}

need_demo_env() {
  # compose refuses to start the screening agent without .demo.env; the other
  # services tolerate its absence but read it when present.
  [[ -f "$HERE/.demo.env" ]] || : > "$HERE/.demo.env"
}

wait_health() {
  local name="$1" url="$2" tries="${3:-60}"
  local i
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS --max-time 3 "$url/health" >/dev/null 2>&1; then
      ok "$name healthy ($url/health)"
      return 0
    fi
    sleep 2
  done
  die "$name did not become healthy at $url/health after $((tries * 2))s"
}

check_health() {
  local name="$1" url="$2"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url/health" 2>/dev/null || true)"
  if [[ "$code" == "200" ]]; then ok "$name $url/health 200"; else warn "$name $url/health ${code:-unreachable}"; fi
}

cmd_provision() {
  need_env
  local extra=()
  if [[ "${EU_STRICT_RESIDENCY:-0}" == "1" ]]; then extra+=(--residency); fi
  log "Provisioning through the control plane (${CP_URL:-http://localhost:5050})"
  "$PYTHON" "$HERE/setup.py" ${extra[@]+"${extra[@]}"} "$@"
}

cmd_up() {
  need_env
  need_network
  need_demo_env
  log "Building and starting the MCP servers and the fraud agent"
  "${COMPOSE[@]}" up -d --build applications-mcp credit-bureau-mcp fraud-screener-agent
  log "Waiting for health"
  wait_health "applications-mcp" "$APPLICATIONS_URL"
  wait_health "credit-bureau-mcp" "$BUREAU_URL"
  wait_health "fraud-screener-agent" "$FRAUD_URL"
  cmd_provision "$@"
  # The fraud agent read .demo.env at start; if its key was minted just now it
  # needs a restart to pick it up.
  log "Restarting the fraud agent with its API key"
  need_env   # re-export: .demo.env now holds FRAUD_BRUTOR_API_KEY
  "${COMPOSE[@]}" up -d --force-recreate fraud-screener-agent
  wait_health "fraud-screener-agent" "$FRAUD_URL"
  cmd_start
}

cmd_start() {
  need_env
  need_network
  [[ -s "$HERE/.demo.env" ]] || die ".demo.env is empty; run ./demo.sh provision first"
  log "Starting the screening agent"
  "${COMPOSE[@]}" --profile agent up -d --build --no-deps screening-agent
  wait_health "screening-agent" "$SCREENING_URL" 45
  log "First tick runs at start; one run per pending application. Watch: ./demo.sh logs screening-agent"
}

cmd_status() {
  need_env
  log "Containers"
  "${COMPOSE[@]}" --profile agent ps || true
  log "Health"
  check_health "applications-mcp" "$APPLICATIONS_URL"
  check_health "credit-bureau-mcp" "$BUREAU_URL"
  check_health "fraud-screener-agent" "$FRAUD_URL"
  check_health "screening-agent" "$SCREENING_URL"
  log "Gateway view"
  "$PYTHON" "$HERE/verify.py" --brief || true
}

cmd_logs() {
  "${COMPOSE[@]}" --profile agent logs -f --tail=200 "$@"
}

# One JSON-RPC tools/call against the applications MCP (stateless streamable
# HTTP, json_response on, so no initialize handshake is needed; same shape as
# brutor-demo-applications-mcp/smoke.sh). Prints the response body.
mcp_call() {
  local tool="$1" args="${2:-}"
  [[ -n "$args" ]] || args='{}'
  curl -sS --fail --max-time 10 -X POST "$APPLICATIONS_URL/mcp" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}"
}

first_pending_id() {
  # The tool returns the JSON array as one text item; the id pattern is
  # APP-<yyyymmdd>-<seq>, so a grep on the raw body is enough (no jq needed).
  # Skip applications the agent already holds for an underwriter (the agent
  # refuses them anyway); the store lives in the agent container's /data.
  local body held id
  body="$(mcp_call applications_list_pending '{"limit":50}' 2>/dev/null || true)"
  held="$(docker exec brutor-demo-screening-agent cat /data/pending_approvals.json 2>/dev/null \
          | grep -o 'APP-[0-9]\{8\}-[0-9]\{1,\}' | sort -u || true)"
  for id in $(printf '%s' "$body" | grep -o 'APP-[0-9]\{8\}-[0-9]\{1,\}'); do
    if ! printf '%s\n' "$held" | grep -qx "$id"; then
      printf '%s' "$id"
      return 0
    fi
  done
  return 0
}

cmd_run_one() {
  local app_id="${1:-}"
  docker inspect -f '{{.State.Running}}' brutor-demo-screening-agent 2>/dev/null | grep -q true \
    || die "brutor-demo-screening-agent is not running; ./demo.sh start first"
  if [[ -z "$app_id" ]]; then
    app_id="$(first_pending_id)"
    [[ -n "$app_id" ]] || die "no pending application at $APPLICATIONS_URL/mcp; try ./demo.sh generate 1"
    log "Screening the first pending application $app_id"
  else
    log "Screening $app_id"
  fi
  # One process, one run: `--application` never starts the health server, so
  # it does not collide with the scheduler already running in the container.
  docker exec brutor-demo-screening-agent python -m screening_agent --application "$app_id"
}

cmd_generate() {
  local count="${1:-1}"
  [[ "$count" =~ ^[0-9]+$ && "$count" -ge 1 ]] || die "usage: ./demo.sh generate N (N >= 1)"
  docker inspect -f '{{.State.Running}}' brutor-demo-applications-mcp 2>/dev/null | grep -q true \
    || die "brutor-demo-applications-mcp is not running; ./demo.sh up first"
  log "Adding $count synthetic application(s)"
  docker exec brutor-demo-applications-mcp python -m applications_mcp.generate --count "$count"
  ok "the next tick (or ./demo.sh run-one) picks them up"
}

cmd_down() {
  local args=()
  if [[ "${1:-}" == "--volumes" ]]; then args+=(--volumes); fi
  "${COMPOSE[@]}" --profile agent down ${args[@]+"${args[@]}"}
  ok "stopped${1:+ (volumes removed)}"
}

usage() {
  sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
  up)        shift; cmd_up "$@" ;;
  provision) shift; cmd_provision "$@" ;;
  start)     shift; cmd_start ;;
  status)    shift; cmd_status ;;
  logs)      shift; cmd_logs "$@" ;;
  down)      shift; cmd_down "$@" ;;
  run-one)   shift; cmd_run_one "${1:-}" ;;
  generate)  shift; cmd_generate "${1:-}" ;;
  ""|-h|--help|help) usage ;;
  *) usage; die "unknown command: $1" ;;
esac
