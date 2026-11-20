#!/usr/bin/env bash
# demo.sh: orchestrate the Brutor Demo System (DESIGN.md section 5.6).
#
# Runs against Brutor Agent Control Community Edition (or Enterprise), started
# from its download with Docker Compose.
#
#   ./demo.sh up          start SPIRE and register the agents' workload
#                         identities, build + start the MCP servers and the
#                         fraud agent, wait for their /health, run setup.py,
#                         start the screening agent
#   ./demo.sh spire       start SPIRE only: server, OIDC Discovery Provider,
#                         agent (attested with a one-time join token), and the
#                         registration entries of the two agents
#   ./demo.sh provision   run setup.py only
#   ./demo.sh start       start (or restart) the screening agent
#   ./demo.sh status      curl the four /health endpoints + verify.py --brief
#   ./demo.sh logs [svc]  follow container logs
#   ./demo.sh down        stop the containers (keeps volumes; --volumes removes them)
#   ./demo.sh run-one [APP-id]
#                         process exactly one pending application on the running
#                         agent (default: the first id from applications_list_pending)
#   ./demo.sh approve [ID ...] | --all [--wait SECONDS] | --reject ID [...]
#                         held decisions, decided as the administrator through
#                         the Control Plane (approve.py): no arguments lists
#                         them, --all approves every pending one (unattended
#                         runs), --reject hands the application to manual
#                         review. The same decision as Mission Control ->
#                         Approvals in the Admin Console
#   ./demo.sh resolve     run one agent tick now, so decisions are applied
#                         without waiting for the next scheduled tick
#   ./demo.sh generate N  add N synthetic applications to the origination mock
#   ./demo.sh copied-key  the agents' calls made with their development-stage
#                         API keys instead of their JWT-SVIDs (copied_key.py):
#                         an AI System in production must refuse every one
#   ./demo.sh requeue APP-id [APP-id ...] | --all-skipped
#                         put applications the agent handed off to manual review
#                         (status needs_manual_review) back in the queue and
#                         clear them from the agent's skip list; --all-skipped
#                         re-queues every application on the agent's skip list
#
# Any extra arguments after `up` / `provision` are passed to setup.py
# (for example: ./demo.sh up --residency). `--portal-approver` is the
# Enterprise Edition variant: it also creates the underwriter as a User Portal
# user, and stops at once on a Community Edition gateway.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

COMPOSE_FILE="$HERE/docker-compose.yml"
PYTHON="${PYTHON:-python3}"

# The demo's SPIFFE trust domain (spire/server.conf) and the agents' workload
# identities: <value of the io.brutor.demo.workload label> <user id> <SPIFFE ID>.
# setup.py registers the same SPIFFE IDs as the agent identities' subjects.
SPIRE_TRUST_DOMAIN="borealis.example"
SPIRE_NODE_ID="spiffe://$SPIRE_TRUST_DOMAIN/brutor-demo/node"
SPIRE_WORKLOADS=(
  "screening-agent 10101 spiffe://$SPIRE_TRUST_DOMAIN/brutor-demo/screening-agent"
  "fraud-screener-agent 10102 spiffe://$SPIRE_TRUST_DOMAIN/brutor-demo/fraud-screener-agent"
)

# Settings that .env may give: derived again once need_env has loaded it.
derive_settings() {
  NETWORK="${BRUTOR_NETWORK:-brutor-network}"
  COMPOSE=(docker compose -f "$COMPOSE_FILE")
  # SPIRE's workload attestor: docker (default; the SPIRE Agent reads the Docker
  # socket) or unix (the fallback without it; spire/README.md).
  SPIRE_WORKLOAD_ATTESTOR="${SPIRE_WORKLOAD_ATTESTOR:-docker}"
  case "$SPIRE_WORKLOAD_ATTESTOR" in
    docker) ;;
    unix) COMPOSE+=(-f "$HERE/spire/docker-compose.unix-attestor.yml") ;;
    *) printf 'SPIRE_WORKLOAD_ATTESTOR must be docker or unix, not %s\n' "$SPIRE_WORKLOAD_ATTESTOR" >&2; exit 1 ;;
  esac
  APPLICATIONS_URL="${APPLICATIONS_HOST_URL:-http://127.0.0.1:${APPLICATIONS_HOST_PORT:-3014}}"
  BUREAU_URL="${BUREAU_HOST_URL:-http://127.0.0.1:${BUREAU_HOST_PORT:-3015}}"
  FRAUD_URL="${FRAUD_HOST_URL:-http://127.0.0.1:${FRAUD_HOST_PORT:-9200}}"
  SCREENING_URL="${SCREENING_HOST_URL:-http://127.0.0.1:${SCREENING_HOST_PORT:-9201}}"
}
derive_settings

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
  derive_settings
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
    die "docker network '$NETWORK' does not exist. Start Brutor Agent Control first (docker compose up -d in its folder creates the network). If your stack's network has another name, set BRUTOR_NETWORK in .env (docker network ls)."
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

wait_url() {
  local name="$1" url="$2" tries="${3:-60}" i
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS --max-time 3 -o /dev/null "$url" 2>/dev/null; then
      ok "$name answers ($url)"
      return 0
    fi
    sleep 2
  done
  die "$name did not answer at $url after $((tries * 2))s"
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
  log "Provisioning through the Control Plane (${CP_URL:-http://localhost:5050})"
  "$PYTHON" "$HERE/setup.py" ${extra[@]+"${extra[@]}"} "$@"
}

spire_server() {
  "${COMPOSE[@]}" exec -T spire-server /opt/spire/bin/spire-server "$@"
}

cmd_spire() {
  need_env
  need_network
  log "Starting SPIRE: server and OIDC Discovery Provider"
  "${COMPOSE[@]}" up -d --wait spire-server spire-oidc
  wait_url "spire-oidc" "http://127.0.0.1:${SPIRE_OIDC_HOST_PORT:-9208}/keys"
  # The agent trusts the server by its CA bundle (public), not by a first contact.
  mkdir -p "$HERE/spire/runtime"
  spire_server bundle show -format pem > "$HERE/spire/runtime/bundle.pem"
  if [[ "$(docker inspect -f '{{.State.Health.Status}}' brutor-demo-spire-agent 2>/dev/null || true)" == "healthy" ]]; then
    # Already attested: it renews its own identity from its data volume. Brought
    # up again so a changed configuration (SPIRE_WORKLOAD_ATTESTOR) applies.
    "${COMPOSE[@]}" up -d --wait spire-agent
    ok "spire-agent attested"
  else
    log "Attesting the SPIRE Agent with a one-time join token"
    # The token stays in this shell; it is single-use and expires in 10 minutes.
    local token
    token="$(spire_server token generate -spiffeID "$SPIRE_NODE_ID" -ttl 600 | sed -n 's/^Token: //p')"
    [[ -n "$token" ]] || die "spire-server generated no join token"
    SPIRE_JOIN_TOKEN="$token" "${COMPOSE[@]}" up -d --force-recreate --wait spire-agent
    token=""
    ok "spire-agent attested as $SPIRE_NODE_ID"
  fi
  log "Registering the agents' workload identities ($SPIRE_WORKLOAD_ATTESTOR attestor)"
  local w label uid id selector
  for w in "${SPIRE_WORKLOADS[@]}"; do
    read -r label uid id <<<"$w"
    if [[ "$SPIRE_WORKLOAD_ATTESTOR" == "unix" ]]; then
      selector="unix:uid:$uid"
    else
      selector="docker:label:io.brutor.demo.workload:$label"
    fi
    if spire_server entry show -spiffeID "$id" -selector "$selector" | grep -q "^Entry ID"; then
      ok "$id ($selector)"
    else
      spire_server entry create -parentID "$SPIRE_NODE_ID" -spiffeID "$id" -selector "$selector" >/dev/null \
        || die "could not register $id"
      ok "$id ($selector) registered"
    fi
  done
}

cmd_up() {
  need_env
  need_network
  need_demo_env
  cmd_spire
  log "Building and starting the MCP servers and the fraud agent"
  "${COMPOSE[@]}" up -d --build applications-mcp credit-bureau-mcp fraud-screener-agent
  log "Waiting for health"
  wait_health "applications-mcp" "$APPLICATIONS_URL"
  wait_health "credit-bureau-mcp" "$BUREAU_URL"
  wait_health "fraud-screener-agent" "$FRAUD_URL"
  cmd_provision "$@"
  # The fraud agent read .demo.env at start; restart it so it runs with the
  # credential setup.py wrote there (BRUTOR_CREDENTIAL).
  log "Restarting the fraud agent with .demo.env"
  need_env   # re-export: .demo.env now holds BRUTOR_CREDENTIAL
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

cmd_copied_key() {
  need_env
  "$PYTHON" "$HERE/copied_key.py" "$@"
}

cmd_approve() {
  need_env
  "$PYTHON" "$HERE/approve.py" "$@"
}

cmd_resolve() {
  need_running brutor-demo-screening-agent
  log "One tick now: the agent polls its held decisions and applies what was decided"
  # `--once` runs one whole tick in its own process and exits (it never starts
  # the health server): held decisions are polled first, then up to
  # MAX_PER_TICK pending applications are screened, as on a scheduled tick.
  docker exec brutor-demo-screening-agent python -m screening_agent --once
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

need_running() {
  docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true \
    || die "$1 is not running; ./demo.sh up first"
}

cmd_requeue() {
  [[ $# -ge 1 ]] || die "usage: ./demo.sh requeue APP-id [APP-id ...] | --all-skipped"
  need_running brutor-demo-applications-mcp
  need_running brutor-demo-screening-agent
  local ids=() out id
  if [[ "$1" == "--all-skipped" ]]; then
    [[ $# -eq 1 ]] || die "--all-skipped takes no ids"
    log "Clearing the agent's skip list"
    out="$(docker exec brutor-demo-screening-agent python -m screening_agent --requeue-all-skipped)"
    printf '%s\n' "$out"
    for id in $(printf '%s\n' "$out" | grep 'released from the skip list' | grep -o 'APP-[0-9]\{8\}-[0-9]\{1,\}'); do
      ids+=("$id")
    done
    if [[ ${#ids[@]} -eq 0 ]]; then
      ok "nothing was skipped"
      return 0
    fi
  else
    for id in "$@"; do
      [[ "$id" =~ ^APP-[0-9]{8}-[0-9]+$ ]] || die "not an application id: $id"
      ids+=("$id")
    done
    log "Clearing the agent's skip list"
    docker exec brutor-demo-screening-agent python -m screening_agent --requeue "${ids[@]}"
  fi
  log "Moving them back to received in the origination system"
  # Exit 1 means an id was not found; the rest were still re-queued.
  docker exec brutor-demo-applications-mcp python -m applications_mcp.requeue "${ids[@]}" || true
  ok "the next tick screens them again (./demo.sh logs screening-agent)"
}

cmd_down() {
  local args=()
  if [[ "${1:-}" == "--volumes" ]]; then args+=(--volumes); fi
  "${COMPOSE[@]}" --profile agent down ${args[@]+"${args[@]}"}
  if [[ "${1:-}" == "--volumes" ]]; then rm -f "$HERE/spire/runtime/bundle.pem"; fi
  ok "stopped${1:+ (volumes removed)}"
}

usage() {
  sed -n '2,44p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
  up)        shift; cmd_up "$@" ;;
  spire)     shift; cmd_spire ;;
  copied-key) shift; cmd_copied_key "$@" ;;
  provision) shift; cmd_provision "$@" ;;
  start)     shift; cmd_start ;;
  status)    shift; cmd_status ;;
  logs)      shift; cmd_logs "$@" ;;
  down)      shift; cmd_down "$@" ;;
  run-one)   shift; cmd_run_one "${1:-}" ;;
  approve)   shift; cmd_approve "$@" ;;
  resolve)   shift; cmd_resolve ;;
  generate)  shift; cmd_generate "${1:-}" ;;
  requeue)   shift; cmd_requeue "$@" ;;
  ""|-h|--help|help) usage ;;
  *) usage; die "unknown command: $1" ;;
esac
