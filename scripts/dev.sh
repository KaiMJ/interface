#!/usr/bin/env bash
#
# Run all three services in one terminal, with prefixed output and a clean
# teardown. `make dev`.
#
#   targetapp  :8080   the mock bank
#   console    :3000   operator UI
#   api        :8000   control plane
#
# Ctrl-C stops all three. Three implementation details are load-bearing:
#
#   1. Each service is started via process substitution, not a pipe. With
#      `cmd | sed &`, `$!` is the PID of *sed* — killing it leaves the server
#      running and the port held.
#   2. `exec` inside each subshell means the subshell becomes the server, so
#      there is no intermediate shell to orphan it.
#   3. Teardown walks the process tree. `pnpm dev` spawns `next` which spawns
#      `node`; signalling only the direct child leaves a live listener behind.
#      This is the failure you actually hit, and it is invisible until the next
#      start fails with EADDRINUSE.
#
# `docker compose up` is the real deployment target; this is the fast inner loop
# and it gives you no X display and no VNC.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

declare -A PORTS=( [targetapp]=8080 [console]=3000 [api]=8000 )
pids=()

c_reset=$'\033[0m'; c_dim=$'\033[2m'
c_target=$'\033[36m'; c_console=$'\033[35m'; c_api=$'\033[33m'; c_warn=$'\033[31m'

log()  { printf '%s[dev]%s %s\n' "$c_dim" "$c_reset" "$*"; }
warn() { printf '%s[dev]%s %s\n' "$c_warn" "$c_reset" "$*"; }

port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

# Depth-first: signal children before their parent, so a supervisor cannot
# reap-and-respawn on the way down.
kill_tree() {
  local pid=$1 sig=$2 child
  for child in $(pgrep -P "$pid" 2>/dev/null); do kill_tree "$child" "$sig"; done
  kill -"$sig" "$pid" 2>/dev/null
}

any_alive() {
  local pid
  for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null && return 0; done
  return 1
}

cleanup() {
  trap - EXIT INT TERM
  printf '\n'
  log "stopping…"

  local pid
  for pid in "${pids[@]}"; do kill_tree "$pid" TERM; done
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    any_alive || break
    sleep 0.5
  done
  for pid in "${pids[@]}"; do kill_tree "$pid" KILL; done
  wait 2>/dev/null

  # Verify rather than assume. A port still held after teardown is the whole bug
  # class this function exists to prevent, so say so instead of exiting 0.
  sleep 0.5
  local name stuck=()
  for name in "${!PORTS[@]}"; do
    port_open "${PORTS[$name]}" && stuck+=("$name:${PORTS[$name]}")
  done
  if [ ${#stuck[@]} -gt 0 ]; then
    warn "ports still held after teardown: ${stuck[*]}"
    warn "find it with:  ss -ltnp | grep -E ':(8080|3000|8000) '"
    return 1
  fi
  log "stopped."
}
trap cleanup EXIT INT TERM

# Fail before starting anything. A half-started stack is more confusing than
# none, and the usual cause is a previous run that did not shut down.
busy=()
for name in "${!PORTS[@]}"; do
  port_open "${PORTS[$name]}" && busy+=("$name:${PORTS[$name]}")
done
if [ ${#busy[@]} -gt 0 ]; then
  warn "ports already in use: ${busy[*]}"
  warn "stop the other stack first (\`docker compose down\`, or kill the process holding the port)"
  trap - EXIT
  exit 1
fi

start() {
  local label=$1 colour=$2 dir=$3
  shift 3
  ( cd "$dir" && exec "$@" ) \
    > >(sed -u "s/^/${colour}$(printf '%-9s' "$label")${c_reset}│ /") 2>&1 &
  pids+=("$!")
}

log "targetapp :8080 · console :3000 · api :8000   —  Ctrl-C stops all three"

start targetapp "$c_target"  targetapp pnpm dev
start console   "$c_console" console   pnpm dev
start api       "$c_api"     backend   uv run uvicorn cua.api.main:app --reload --port 8000

wait
