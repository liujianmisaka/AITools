#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ps_script="$(cygpath -w "$script_dir/stop-multi-agent-v2-dev.ps1")"
args=()

while (($#)); do
  case "$1" in
    --keep-infrastructure) args+=("-KeepInfrastructure"); shift ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

exec powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$ps_script" "${args[@]}"
