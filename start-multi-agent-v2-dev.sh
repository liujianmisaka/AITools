#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ps_script="$(cygpath -w "$script_dir/start-multi-agent-v2-dev.ps1")"
args=()

while (($#)); do
  case "$1" in
    --detached) args+=("-Detached"); shift ;;
    --skip-frontend-install) args+=("-SkipFrontendInstall"); shift ;;
    --public-host) args+=("-PublicHost" "$2"); shift 2 ;;
    --core-port) args+=("-CorePort" "$2"); shift 2 ;;
    --web-port) args+=("-WebPort" "$2"); shift 2 ;;
    --internal-port) args+=("-InternalPort" "$2"); shift 2 ;;
    --frontend-port) args+=("-FrontendPort" "$2"); shift 2 ;;
    --workspace-id) args+=("-WorkspaceId" "$2"); shift 2 ;;
    --workspace-config)
      args+=("-WorkspaceConfigPath" "$(cygpath -w "$2")")
      shift 2
      ;;
    --heartbeat-seconds) args+=("-HeartbeatSeconds" "$2"); shift 2 ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

exec powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$ps_script" "${args[@]}"
