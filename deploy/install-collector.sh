#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install -Dm755 "$project_dir/collector.py" "$HOME/.local/share/codex-usage-collector/collector.py"
install -Dm644 "$project_dir/deploy/codex-usage-collector.service" "$HOME/.config/systemd/user/codex-usage-collector.service"

if [[ ! -f "$HOME/.config/codex-usage-collector.env" ]]; then
  echo "Missing $HOME/.config/codex-usage-collector.env" >&2
  echo "Create it with CODEX_MONITOR_SERVER and CODEX_COLLECTOR_TOKEN_FILE first." >&2
  exit 1
fi

systemctl --user daemon-reload
systemctl --user enable --now codex-usage-collector.service
systemctl --user status codex-usage-collector.service --no-pager
