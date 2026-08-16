#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ps_script="$(cygpath -w "$script_dir/stop-multi-agent-v2-dev.ps1")"
exec powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$ps_script"
