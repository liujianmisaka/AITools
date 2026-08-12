#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

quiet="false"

usage() {
  cat <<'EOF'
Usage: ./stop-multi-agent-dev.sh [--quiet]

Stops only the Git Bash development services recorded by
.multi-agent-dev/processes.git-bash.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quiet)
      quiet="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Error: unknown option: %s\n' "$1" >&2
      exit 1
      ;;
  esac
done

command -v powershell.exe >/dev/null 2>&1 || {
  printf 'Error: powershell.exe is required for safe PID identity checks.\n' >&2
  exit 1
}
command -v taskkill.exe >/dev/null 2>&1 || {
  printf 'Error: taskkill.exe is required for stopping reload process trees.\n' >&2
  exit 1
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
runtime_dir="$script_dir/.multi-agent-dev"
manifest_path="$runtime_dir/processes.git-bash"
stopping_marker="$manifest_path.stopping"

if [[ ! -f "$manifest_path" ]]; then
  if [[ "$quiet" != "true" ]]; then
    printf 'No Git Bash runtime manifest was found; no services are tracked: %s\n' "$manifest_path"
  fi
  exit 0
fi

schema_version=""
launcher=""
core_pid=""
core_name=""
core_start_ticks=""
core_executable_b64=""
core_port=""
web_pid=""
web_name=""
web_start_ticks=""
web_executable_b64=""
web_port=""
frontend_pid=""
frontend_name=""
frontend_start_ticks=""
frontend_executable_b64=""
frontend_port=""

while IFS='=' read -r key value; do
  value="${value//$'\r'/}"
  case "$key" in
    schema_version) schema_version="$value" ;;
    launcher) launcher="$value" ;;
    core_pid) core_pid="$value" ;;
    core_name) core_name="$value" ;;
    core_start_ticks) core_start_ticks="$value" ;;
    core_executable_b64) core_executable_b64="$value" ;;
    core_port) core_port="$value" ;;
    web_pid) web_pid="$value" ;;
    web_name) web_name="$value" ;;
    web_start_ticks) web_start_ticks="$value" ;;
    web_executable_b64) web_executable_b64="$value" ;;
    web_port) web_port="$value" ;;
    frontend_pid) frontend_pid="$value" ;;
    frontend_name) frontend_name="$value" ;;
    frontend_start_ticks) frontend_start_ticks="$value" ;;
    frontend_executable_b64) frontend_executable_b64="$value" ;;
    frontend_port) frontend_port="$value" ;;
  esac
done <"$manifest_path"

[[ "$schema_version" == "3" ]] || {
  printf 'Error: unsupported Git Bash runtime manifest version; the manifest was preserved: %s\n' "$manifest_path" >&2
  exit 1
}
[[ "$launcher" == "git-bash" ]] || {
  printf 'Error: runtime manifest launcher mismatch; the manifest was preserved: %s\n' "$manifest_path" >&2
  exit 1
}

for numeric_value in "$core_pid" "$core_start_ticks" "$core_port" "$web_pid" "$web_start_ticks" "$web_port" "$frontend_pid" "$frontend_start_ticks" "$frontend_port"; do
  [[ "$numeric_value" =~ ^[0-9]+$ ]] || {
    printf 'Error: runtime manifest contains an invalid numeric field; the manifest was preserved.\n' >&2
    exit 1
  }
done
[[ -n "$core_name" && -n "$core_executable_b64" && -n "$web_name" && -n "$web_executable_b64" && -n "$frontend_name" && -n "$frontend_executable_b64" ]] || {
  printf 'Error: runtime manifest is incomplete; the manifest was preserved.\n' >&2
  exit 1
}

remove_stopping_marker() {
  rm -f -- "$stopping_marker"
}

printf '%s\n' "$$" >"$stopping_marker"
trap remove_stopping_marker EXIT

get_process_identity() {
  local process_id="$1"
  local result
  if ! result="$(MSYS2_ARG_CONV_EXCL='*' powershell.exe -NoProfile -NonInteractive -Command "\$p = Get-Process -Id $process_id -ErrorAction Stop; \$path = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(\$p.Path)); Write-Output (('{0}|{1}|{2}' -f \$p.ProcessName, \$p.StartTime.ToUniversalTime().Ticks, \$path))" 2>/dev/null)"; then
    return 1
  fi
  printf '%s\n' "${result//$'\r'/}"
}

stop_tracked_service() {
  local service_name="$1"
  local process_id="$2"
  local expected_name="$3"
  local expected_start_ticks="$4"
  local expected_executable_b64="$5"
  local current_identity=""
  local actual_name=""
  local actual_start_ticks=""
  local actual_executable_b64=""

  if ! current_identity="$(get_process_identity "$process_id")"; then
    if [[ "$quiet" != "true" ]]; then
      printf '%s is already stopped (recorded PID %s does not exist).\n' "$service_name" "$process_id"
    fi
    return 0
  fi

  IFS='|' read -r actual_name actual_start_ticks actual_executable_b64 <<<"$current_identity"
  if [[ "$actual_name" != "$expected_name" ]]; then
    printf 'Warning: %s PID %s was reused; process name does not match.\n' "$service_name" "$process_id" >&2
    return 1
  fi
  if [[ "$actual_start_ticks" != "$expected_start_ticks" ]]; then
    printf 'Warning: %s PID %s was reused; start time does not match.\n' "$service_name" "$process_id" >&2
    return 1
  fi
  if [[ "$actual_executable_b64" != "$expected_executable_b64" ]]; then
    printf 'Warning: %s PID %s was reused; executable path does not match.\n' "$service_name" "$process_id" >&2
    return 1
  fi

  local taskkill_output=""
  local taskkill_status=0
  if taskkill_output="$(MSYS2_ARG_CONV_EXCL='*' taskkill.exe /PID "$process_id" /T /F 2>&1)"; then
    taskkill_status=0
  else
    taskkill_status=$?
  fi

  local attempt
  for attempt in {1..40}; do
    if ! get_process_identity "$process_id" >/dev/null 2>&1; then
      if [[ "$quiet" != "true" ]]; then
        printf '%s stopped (root PID %s).\n' "$service_name" "$process_id"
      fi
      return 0
    fi
    sleep 0.2
  done

  if (( taskkill_status != 0 )); then
    printf 'Warning: failed to stop %s: %s\n' "$service_name" "${taskkill_output//$'\r'/ }" >&2
  else
    printf 'Warning: %s root PID %s still exists after taskkill.\n' "$service_name" "$process_id" >&2
  fi
  return 1
}

web_stopped="false"
core_stopped="false"
frontend_stopped="false"
if stop_tracked_service "React frontend" "$frontend_pid" "$frontend_name" "$frontend_start_ticks" "$frontend_executable_b64"; then
  frontend_stopped="true"
fi
if stop_tracked_service "Web service" "$web_pid" "$web_name" "$web_start_ticks" "$web_executable_b64"; then
  web_stopped="true"
fi
if stop_tracked_service "Core service" "$core_pid" "$core_name" "$core_start_ticks" "$core_executable_b64"; then
  core_stopped="true"
fi

if [[ "$frontend_stopped" != "true" || "$web_stopped" != "true" || "$core_stopped" != "true" ]]; then
  printf 'Error: at least one service was not safely stopped; the runtime manifest was preserved: %s\n' "$manifest_path" >&2
  exit 1
fi

rm -f -- "$manifest_path"
rm -f -- "$stopping_marker"
trap - EXIT

port_is_open() {
  local port="$1"
  MSYS2_ARG_CONV_EXCL='*' powershell.exe -NoProfile -NonInteractive -Command "\$client = [Net.Sockets.TcpClient]::new(); try { \$result = \$client.BeginConnect('127.0.0.1', $port, \$null, \$null); if (-not \$result.AsyncWaitHandle.WaitOne(350)) { exit 1 }; \$client.EndConnect(\$result); if (\$client.Connected) { exit 0 }; exit 1 } catch { exit 1 } finally { \$client.Dispose() }" >/dev/null 2>&1
}

if port_is_open "$web_port"; then
  printf 'Warning: Web port %s still accepts connections and may be owned by an untracked process.\n' "$web_port" >&2
fi
if port_is_open "$frontend_port"; then
  printf 'Warning: React frontend port %s still accepts connections and may be owned by an untracked process.\n' "$frontend_port" >&2
fi
if port_is_open "$core_port"; then
  printf 'Warning: core port %s still accepts connections and may be owned by an untracked process.\n' "$core_port" >&2
fi

if [[ "$quiet" != "true" ]]; then
  printf 'Multi-Agent Git Bash development environment stopped. Logs remain in: %s\n' "$runtime_dir"
fi
