#!/usr/bin/env bash
set -euo pipefail

install_root="${CODEX_COLLECTOR_INSTALL_ROOT:-$HOME}"
systemctl --user disable --now codex-usage-collector.service 2>/dev/null || true
rm -f "$install_root/.config/systemd/user/codex-usage-collector.service"
rm -f "$install_root/.config/codex-usage-collector.env"
rm -f "$install_root/.config/codex-usage-collector.token"
rm -f "$install_root/.local/share/codex-usage-collector/collector.py"
rmdir "$install_root/.local/share/codex-usage-collector" 2>/dev/null || true
systemctl --user daemon-reload 2>/dev/null || true
echo "Codex Usage Collector removed for $(id -un)."
