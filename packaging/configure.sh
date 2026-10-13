#!/usr/bin/env bash
set -euo pipefail
app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$app_dir/collector.py" --config-file "${CODEX_COLLECTOR_CONFIG_FILE:-$HOME/.config/collector-config.json}" --configure "$@"
