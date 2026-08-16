#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

listen_host="127.0.0.1"
core_port="8010"
web_port="8020"
frontend_port="5173"
workspace_id="aitools"
workspace_path=""
configured_codex_bin=""
configured_codex_home=""
startup_timeout="30"
heartbeat_seconds="30"
detached="false"

usage() {
  cat <<'EOF'
Usage: ./start-multi-agent-dev.sh [options]

Options:
  --host IP                  Listen address (default: 127.0.0.1)
  --core-port PORT           Core API port (default: 8010)
  --web-port PORT            Web console port (default: 8020)
  --frontend-port PORT       React/Vite port (default: 5173)
  --workspace-id ID          Workspace allowlist ID (default: aitools)
  --workspace-path PATH      Workspace path (default: repository root)
  --codex-bin PATH           Explicit codex.exe path
  --codex-home PATH          Explicit Codex home path
  --startup-timeout SECONDS  Health-check timeout (default: 30)
  --heartbeat-seconds N      Supervised-mode heartbeat interval (default: 30)
  --detached                 Start in background and return after health checks
  -h, --help                 Show this help
EOF
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_value() {
  local option="$1"
  local value="${2:-}"
  [[ -n "$value" ]] || fail "$option requires a value"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      require_value "$1" "${2:-}"
      listen_host="$2"
      shift 2
      ;;
    --core-port)
      require_value "$1" "${2:-}"
      core_port="$2"
      shift 2
      ;;
    --web-port)
      require_value "$1" "${2:-}"
      web_port="$2"
      shift 2
      ;;
    --frontend-port)
      require_value "$1" "${2:-}"
      frontend_port="$2"
      shift 2
      ;;
    --workspace-id)
      require_value "$1" "${2:-}"
      workspace_id="$2"
      shift 2
      ;;
    --workspace-path)
      require_value "$1" "${2:-}"
      workspace_path="$2"
      shift 2
      ;;
    --codex-bin)
      require_value "$1" "${2:-}"
      configured_codex_bin="$2"
      shift 2
      ;;
    --codex-home)
      require_value "$1" "${2:-}"
      configured_codex_home="$2"
      shift 2
      ;;
    --startup-timeout)
      require_value "$1" "${2:-}"
      startup_timeout="$2"
      shift 2
      ;;
    --heartbeat-seconds)
      require_value "$1" "${2:-}"
      heartbeat_seconds="$2"
      shift 2
      ;;
    --detached)
      detached="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

[[ "$core_port" =~ ^[0-9]+$ ]] && (( core_port >= 1 && core_port <= 65535 )) || fail "invalid core port: $core_port"
[[ "$web_port" =~ ^[0-9]+$ ]] && (( web_port >= 1 && web_port <= 65535 )) || fail "invalid web port: $web_port"
[[ "$frontend_port" =~ ^[0-9]+$ ]] && (( frontend_port >= 1 && frontend_port <= 65535 )) || fail "invalid frontend port: $frontend_port"
[[ "$core_port" != "$web_port" && "$core_port" != "$frontend_port" && "$web_port" != "$frontend_port" ]] || fail "core, web BFF, and frontend ports must be different"
[[ "$startup_timeout" =~ ^[0-9]+$ ]] && (( startup_timeout >= 5 && startup_timeout <= 120 )) || fail "startup timeout must be between 5 and 120"
[[ "$heartbeat_seconds" =~ ^[0-9]+$ ]] && (( heartbeat_seconds >= 5 )) || fail "heartbeat interval must be at least 5 seconds"

command -v cygpath >/dev/null 2>&1 || fail "this script must run in Git Bash on Windows"
command -v powershell.exe >/dev/null 2>&1 || fail "powershell.exe is required for safe PID identity checks"
command -v taskkill.exe >/dev/null 2>&1 || fail "taskkill.exe is required for stopping reload process trees"
command -v curl >/dev/null 2>&1 || fail "curl is required for health checks"
command -v npm.cmd >/dev/null 2>&1 || fail "npm.cmd is required for the React development server"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
script_dir_win="$(cygpath -w "$script_dir")"
python_exe="$script_dir/.venv/Scripts/python.exe"
core_app_dir_win="${script_dir_win}\\multi-agent"
core_reload_dir_win="${core_app_dir_win}\\multi_agent"
web_app_dir_win="${script_dir_win}\\multi-agent-web"
web_reload_dir_win="${web_app_dir_win}\\multi_agent_web"
frontend_dir="$script_dir/multi-agent-web/frontend"
frontend_dir_win="${web_app_dir_win}\\frontend"
runtime_dir="$script_dir/.multi-agent-dev"
runtime_dir_win="${script_dir_win}\\.multi-agent-dev"
manifest_path="$runtime_dir/processes.git-bash"
stopping_marker="$manifest_path.stopping"
stop_script="$script_dir/stop-multi-agent-dev.sh"

[[ -x "$python_exe" ]] || fail "shared virtual environment was not found: $python_exe"
[[ -d "$script_dir/multi-agent/multi_agent" ]] || fail "core package directory was not found"
[[ -d "$script_dir/multi-agent-web/multi_agent_web" ]] || fail "web package directory was not found"
[[ -d "$frontend_dir/node_modules" ]] || fail "frontend dependencies were not found; run npm install in multi-agent-web/frontend"
[[ -f "$stop_script" ]] || fail "stop script was not found: $stop_script"

normalize_posix_path() {
  local value="$1"
  if [[ "$value" =~ ^[A-Za-z]:[\\/] ]]; then
    cygpath -u "$value"
  else
    (cd -- "$value" && pwd)
  fi
}

normalize_windows_path() {
  local value="$1"
  if [[ "$value" =~ ^[A-Za-z]:[\\/] ]] || [[ "$value" == \\\\* ]]; then
    printf '%s\n' "$value"
  else
    cygpath -w "$value"
  fi
}

workspace_path="${workspace_path:-$script_dir}"
workspace_posix="$(normalize_posix_path "$workspace_path")" || fail "workspace directory does not exist: $workspace_path"
workspace_win="$(cygpath -w "$workspace_posix")"

get_process_identity() {
  local process_id="$1"
  local result
  if ! result="$(MSYS2_ARG_CONV_EXCL='*' powershell.exe -NoProfile -NonInteractive -Command "\$p = Get-Process -Id $process_id -ErrorAction Stop; \$path = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(\$p.Path)); Write-Output (('{0}|{1}|{2}' -f \$p.ProcessName, \$p.StartTime.ToUniversalTime().Ticks, \$path))" 2>/dev/null)"; then
    return 1
  fi
  printf '%s\n' "${result//$'\r'/}"
}

windows_pid_for_msys_pid() {
  local msys_pid="$1"
  local attempt
  local windows_pid=""
  for attempt in {1..30}; do
    windows_pid="$(ps -W | awk -v target="$msys_pid" 'NR > 1 && $1 == target { print $4; exit }')"
    if [[ "$windows_pid" =~ ^[0-9]+$ ]]; then
      printf '%s\n' "$windows_pid"
      return 0
    fi
    sleep 0.1
  done
  return 1
}

if [[ -f "$manifest_path" ]]; then
  existing_core_pid="$(awk -F= '$1 == "core_pid" { print $2 }' "$manifest_path" | tr -d '\r')"
  existing_web_pid="$(awk -F= '$1 == "web_pid" { print $2 }' "$manifest_path" | tr -d '\r')"
  existing_frontend_pid="$(awk -F= '$1 == "frontend_pid" { print $2 }' "$manifest_path" | tr -d '\r')"
  if [[ -n "$existing_core_pid" ]] && get_process_identity "$existing_core_pid" >/dev/null 2>&1; then
    fail "Git Bash development services already appear to be running (PID $existing_core_pid); run ./stop-multi-agent-dev.sh first"
  fi
  if [[ -n "$existing_web_pid" ]] && get_process_identity "$existing_web_pid" >/dev/null 2>&1; then
    fail "Git Bash development services already appear to be running (PID $existing_web_pid); run ./stop-multi-agent-dev.sh first"
  fi
  if [[ -n "$existing_frontend_pid" ]] && get_process_identity "$existing_frontend_pid" >/dev/null 2>&1; then
    fail "Git Bash development services already appear to be running (PID $existing_frontend_pid); run ./stop-multi-agent-dev.sh first"
  fi
  rm -f -- "$manifest_path"
  rm -f -- "$stopping_marker"
fi
rm -f -- "$stopping_marker"

assert_port_can_bind() {
  local service_name="$1"
  local port="$2"
  if ! MSYS2_ARG_CONV_EXCL='*' "$python_exe" -c '
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
family = socket.AF_INET6 if ":" in host else socket.AF_INET
sock = socket.socket(family, socket.SOCK_STREAM)
try:
    sock.bind((host, port))
finally:
    sock.close()
' "$listen_host" "$port"; then
    fail "$service_name cannot bind ${listen_host}:$port; the port may already be in use"
  fi
}

assert_port_can_bind "core service" "$core_port"
assert_port_can_bind "web service" "$web_port"
assert_port_can_bind "React frontend" "$frontend_port"
mkdir -p -- "$runtime_dir"

session_stamp="$(date '+%Y%m%d-%H%M%S%3N')"
core_log="$runtime_dir/core-git-bash-$session_stamp.log"
web_log="$runtime_dir/web-git-bash-$session_stamp.log"
frontend_log="$runtime_dir/frontend-git-bash-$session_stamp.log"

if [[ -z "$configured_codex_bin" ]]; then
  configured_codex_bin="$(command -v codex.exe 2>/dev/null || command -v codex 2>/dev/null || true)"
fi
if [[ -n "$configured_codex_bin" ]]; then
  configured_codex_bin="$(normalize_windows_path "$configured_codex_bin")"
fi

if [[ -z "$configured_codex_home" ]]; then
  if [[ -n "${CODEX_HOME:-}" ]]; then
    configured_codex_home="$CODEX_HOME"
  elif [[ -n "${USERPROFILE:-}" ]]; then
    configured_codex_home="${USERPROFILE}\\.codex"
  else
    configured_codex_home="$(cygpath -w "$HOME")\\.codex"
  fi
fi
configured_codex_home="$(normalize_windows_path "$configured_codex_home")"

workspace_json="$(MSYS2_ARG_CONV_EXCL='*' "$python_exe" -c 'import json,sys; print(json.dumps({sys.argv[1]: sys.argv[2]}, ensure_ascii=False))' "$workspace_id" "$workspace_win")"
state_database="$runtime_dir/state.sqlite3"
state_database_win="${runtime_dir_win}\\state.sqlite3"
required_state_schema_version="4"
if [[ -f "$state_database" ]]; then
  if ! detected_state_schema_version="$(MSYS2_ARG_CONV_EXCL='*' "$python_exe" -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); row=c.execute("SELECT version FROM schema_metadata WHERE id=1").fetchone(); print(row[0] if row else "missing"); c.close()' "$state_database_win" 2>/dev/null)"; then
    fail "state database schema is unreadable: $state_database. Back up and move the file first; the launcher never migrates or deletes data"
  fi
  [[ "$detected_state_schema_version" == "$required_state_schema_version" ]] || fail "state database schema v$detected_state_schema_version is incompatible with required v$required_state_schema_version: $state_database. Back up and move the file first; the launcher never migrates or deletes data"
fi
export MULTI_AGENT_WORKSPACES="$workspace_json"
export MULTI_AGENT_STATE_DB="$state_database_win"
export MULTI_AGENT_CORE_URL="http://${listen_host}:$core_port"
export VITE_BFF_URL="http://${listen_host}:$web_port"
export PYTHONUNBUFFERED="1"
if [[ -n "$configured_codex_bin" ]]; then
  export MULTI_AGENT_CODEX_BIN="$configured_codex_bin"
fi
if [[ -n "$configured_codex_home" ]]; then
  export MULTI_AGENT_CODEX_HOME="$configured_codex_home"
fi

stop_new_process_tree() {
  local process_id="${1:-}"
  [[ -n "$process_id" ]] || return 0
  if get_process_identity "$process_id" >/dev/null 2>&1; then
    MSYS2_ARG_CONV_EXCL='*' taskkill.exe /PID "$process_id" /T /F >/dev/null 2>&1 || true
  fi
}

core_job_pid=""
web_job_pid=""
frontend_job_pid=""
core_pid=""
web_pid=""
frontend_pid=""

printf 'Starting Multi-Agent core service...\n'
MSYS2_ARG_CONV_EXCL='*' "$python_exe" -m uvicorn multi_agent.main:app \
  --app-dir "$core_app_dir_win" \
  --host "$listen_host" \
  --port "$core_port" \
  --reload \
  --reload-dir "$core_reload_dir_win" \
  --reload-include '*.py' \
  --log-level info >"$core_log" 2>&1 &
core_job_pid=$!
if ! core_pid="$(windows_pid_for_msys_pid "$core_job_pid")"; then
  kill "$core_job_pid" 2>/dev/null || true
  fail "could not map the Git Bash core PID to a Windows PID"
fi

printf 'Starting independent Web service...\n'
MSYS2_ARG_CONV_EXCL='*' "$python_exe" -m uvicorn multi_agent_web.main:app \
  --app-dir "$web_app_dir_win" \
  --host "$listen_host" \
  --port "$web_port" \
  --reload \
  --reload-dir "$web_reload_dir_win" \
  --reload-include '*.py' \
  --reload-include '*.html' \
  --reload-include '*.css' \
  --reload-include '*.js' \
  --log-level info >"$web_log" 2>&1 &
web_job_pid=$!
if ! web_pid="$(windows_pid_for_msys_pid "$web_job_pid")"; then
  kill "$web_job_pid" "$core_job_pid" 2>/dev/null || true
  stop_new_process_tree "$core_pid"
  fail "could not map the Git Bash web PID to a Windows PID"
fi

printf 'Starting React frontend with Vite HMR...\n'
MSYS2_ARG_CONV_EXCL='*' npm.cmd --prefix "$frontend_dir_win" run dev -- \
  --host "$listen_host" \
  --port "$frontend_port" \
  --strictPort >"$frontend_log" 2>&1 &
frontend_job_pid=$!
if ! frontend_pid="$(windows_pid_for_msys_pid "$frontend_job_pid")"; then
  kill "$frontend_job_pid" "$web_job_pid" "$core_job_pid" 2>/dev/null || true
  stop_new_process_tree "$web_pid"
  stop_new_process_tree "$core_pid"
  fail "could not map the Git Bash frontend PID to a Windows PID"
fi

if ! core_identity="$(get_process_identity "$core_pid")"; then
  stop_new_process_tree "$web_pid"
  stop_new_process_tree "$core_pid"
  fail "could not read the core process identity"
fi
if ! web_identity="$(get_process_identity "$web_pid")"; then
  stop_new_process_tree "$frontend_pid"
  stop_new_process_tree "$web_pid"
  stop_new_process_tree "$core_pid"
  fail "could not read the web process identity"
fi
if ! frontend_identity="$(get_process_identity "$frontend_pid")"; then
  stop_new_process_tree "$frontend_pid"
  stop_new_process_tree "$web_pid"
  stop_new_process_tree "$core_pid"
  fail "could not read the frontend process identity"
fi

IFS='|' read -r core_name core_start_ticks core_executable_b64 <<<"$core_identity"
IFS='|' read -r web_name web_start_ticks web_executable_b64 <<<"$web_identity"
IFS='|' read -r frontend_name frontend_start_ticks frontend_executable_b64 <<<"$frontend_identity"

{
  printf 'schema_version=3\n'
  printf 'launcher=git-bash\n'
  printf 'mode=%s\n' "$([[ "$detached" == "true" ]] && printf 'detached' || printf 'supervised')"
  printf 'core_pid=%s\n' "$core_pid"
  printf 'core_name=%s\n' "$core_name"
  printf 'core_start_ticks=%s\n' "$core_start_ticks"
  printf 'core_executable_b64=%s\n' "$core_executable_b64"
  printf 'core_port=%s\n' "$core_port"
  printf 'web_pid=%s\n' "$web_pid"
  printf 'web_name=%s\n' "$web_name"
  printf 'web_start_ticks=%s\n' "$web_start_ticks"
  printf 'web_executable_b64=%s\n' "$web_executable_b64"
  printf 'web_port=%s\n' "$web_port"
  printf 'frontend_pid=%s\n' "$frontend_pid"
  printf 'frontend_name=%s\n' "$frontend_name"
  printf 'frontend_start_ticks=%s\n' "$frontend_start_ticks"
  printf 'frontend_executable_b64=%s\n' "$frontend_executable_b64"
  printf 'frontend_port=%s\n' "$frontend_port"
} >"$manifest_path"

wait_for_health() {
  local service_name="$1"
  local health_url="$2"
  local process_id="$3"
  local log_path="$4"
  local started_at=$SECONDS
  local next_progress=$((SECONDS + 5))

  printf 'Waiting for %s health check: %s\n' "$service_name" "$health_url"
  while (( SECONDS - started_at < startup_timeout )); do
    if ! get_process_identity "$process_id" >/dev/null 2>&1; then
      printf '%s exited during startup. Recent log:\n' "$service_name" >&2
      tail -n 30 -- "$log_path" >&2 || true
      return 1
    fi
    if MSYS2_ARG_CONV_EXCL='*' curl --silent --show-error --fail --max-time 1 "$health_url" 2>/dev/null \
      | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
      printf '%s is ready.\n' "$service_name"
      return 0
    fi
    if (( SECONDS >= next_progress )); then
      printf '%s is still starting...\n' "$service_name"
      next_progress=$((SECONDS + 5))
    fi
    sleep 0.3
  done

  printf '%s did not become healthy within %s seconds. Recent log:\n' "$service_name" "$startup_timeout" >&2
  tail -n 30 -- "$log_path" >&2 || true
  return 1
}

if ! wait_for_health "Core service" "http://${listen_host}:$core_port/health" "$core_pid" "$core_log"; then
  "$stop_script" --quiet || true
  exit 1
fi
if ! wait_for_health "Web service" "http://${listen_host}:$web_port/health" "$web_pid" "$web_log"; then
  "$stop_script" --quiet || true
  exit 1
fi
if ! wait_for_health "React frontend" "http://${listen_host}:$frontend_port/health" "$frontend_pid" "$frontend_log"; then
  "$stop_script" --quiet || true
  exit 1
fi

printf '\nMulti-Agent development environment is ready.\n'
printf '  Core service: http://%s:%s  (PID %s)\n' "$listen_host" "$core_port" "$core_pid"
printf '  Web console:  http://%s:%s  (PID %s)\n' "$listen_host" "$web_port" "$web_pid"
printf '  React page:   http://%s:%s  (PID %s)\n' "$listen_host" "$frontend_port" "$frontend_pid"
printf '  Workspace:    %s -> %s\n' "$workspace_id" "$workspace_win"
printf '  Manifest:     %s\n' "$manifest_path"
printf '  Core log:     %s\n' "$core_log"
printf '  Web log:      %s\n' "$web_log"
printf '  Frontend log: %s\n' "$frontend_log"

if [[ "$detached" == "true" ]]; then
  disown "$core_job_pid" "$web_job_pid" "$frontend_job_pid" 2>/dev/null || true
  printf '\nServices are running in the background. Stop them with: ./stop-multi-agent-dev.sh\n'
  exit 0
fi

supervisor_cleanup() {
  local exit_status=$?
  trap - EXIT INT TERM
  if [[ -f "$manifest_path" ]]; then
    printf '\nStopping Multi-Agent development services...\n'
    "$stop_script" --quiet || printf 'Warning: automatic cleanup failed; run ./stop-multi-agent-dev.sh manually.\n' >&2
  fi
  exit "$exit_status"
}

trap supervisor_cleanup EXIT
trap 'exit 130' INT TERM

printf '\nSupervised mode is active; Python uses reload and React uses Vite HMR.\n'
printf 'Press Ctrl+C to stop all three services, or run ./stop-multi-agent-dev.sh in another terminal.\n'

next_heartbeat=$((SECONDS + heartbeat_seconds))
while :; do
  if [[ ! -f "$manifest_path" ]]; then
    printf 'Services were stopped by the stop script.\n'
    break
  fi
  if [[ -f "$stopping_marker" ]]; then
    sleep 0.2
    continue
  fi
  if ! kill -0 "$core_job_pid" 2>/dev/null; then
    printf 'Core service exited unexpectedly. Recent log:\n' >&2
    tail -n 30 -- "$core_log" >&2 || true
    exit 1
  fi
  if ! kill -0 "$web_job_pid" 2>/dev/null; then
    printf 'Web service exited unexpectedly. Recent log:\n' >&2
    tail -n 30 -- "$web_log" >&2 || true
    exit 1
  fi
  if ! kill -0 "$frontend_job_pid" 2>/dev/null; then
    printf 'React frontend exited unexpectedly. Recent log:\n' >&2
    tail -n 30 -- "$frontend_log" >&2 || true
    exit 1
  fi
  if (( SECONDS >= next_heartbeat )); then
    printf '[%s] Core, Web BFF, and React frontend are running.\n' "$(date '+%H:%M:%S')"
    next_heartbeat=$((SECONDS + heartbeat_seconds))
  fi
  sleep 1
done
