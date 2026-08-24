#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./install.sh [--server URL] [--token-file PATH] [--account-key KEY] [--no-start]

Installs Codex Usage Collector for the current OS user. Root is not required.
If the bundle contains collector.token, --token-file is optional.
EOF
}

bundle_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install_root="${CODEX_COLLECTOR_INSTALL_ROOT:-$HOME}"
server=""
token_file=""
account_key=""
start_service=true

if [[ -f "$bundle_dir/bundle.conf" ]]; then
  server="$(sed -n 's/^server=//p' "$bundle_dir/bundle.conf" | head -n 1)"
  account_key="$(sed -n 's/^account_key=//p' "$bundle_dir/bundle.conf" | head -n 1)"
fi
if [[ -f "$bundle_dir/collector.token" ]]; then
  token_file="$bundle_dir/collector.token"
fi

while (($#)); do
  case "$1" in
    --server)
      [[ $# -ge 2 ]] || { echo "--server requires a value" >&2; exit 2; }
      server="$2"; shift 2 ;;
    --token-file)
      [[ $# -ge 2 ]] || { echo "--token-file requires a value" >&2; exit 2; }
      token_file="$2"; shift 2 ;;
    --account-key)
      [[ $# -ge 2 ]] || { echo "--account-key requires a value" >&2; exit 2; }
      account_key="$2"; shift 2 ;;
    --no-start) start_service=false; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$server" == http://* || "$server" == https://* ]] || {
  echo "A valid --server URL is required" >&2; exit 2;
}
[[ -n "$token_file" && -f "$token_file" ]] || {
  echo "Collector token not found. Pass --token-file PATH." >&2; exit 2;
}
[[ "$account_key" =~ ^[a-fA-F0-9]{12}$ ]] || {
  echo "A valid registered account key is required." >&2; exit 2;
}
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

app_dir="$install_root/.local/share/codex-usage-collector"
config_dir="$install_root/.config"
service_dir="$config_dir/systemd/user"
umask 077
install -d -m 700 "$app_dir" "$service_dir"
install -m 755 "$bundle_dir/collector.py" "$app_dir/collector.py"
install -m 644 "$bundle_dir/codex-usage-collector.service" "$service_dir/codex-usage-collector.service"
install -m 600 "$token_file" "$config_dir/codex-usage-collector.token"
printf 'CODEX_MONITOR_SERVER=%s\nCODEX_COLLECTOR_TOKEN_FILE=%s\nCODEX_TARGET_ACCOUNT_KEY=%s\n' \
  "$server" "$config_dir/codex-usage-collector.token" "$account_key" > "$config_dir/codex-usage-collector.env"
chmod 600 "$config_dir/codex-usage-collector.env"

echo "Installed collector for $(id -un)"
echo "Central server: $server"

if [[ "$start_service" == true ]]; then
  if systemctl --user daemon-reload && \
     systemctl --user enable codex-usage-collector.service && \
     systemctl --user restart codex-usage-collector.service; then
    systemctl --user status codex-usage-collector.service --no-pager || true
  else
    echo "Could not start the user service." >&2
    echo "You can test manually with:" >&2
    echo "  python3 $app_dir/collector.py --server $server --token-file $config_dir/codex-usage-collector.token --account-key $account_key --once" >&2
    exit 1
  fi
else
  echo "Files installed; service start skipped."
fi
