#!/usr/bin/env python3
"""Machine-wide Windows collector intended to run as SYSTEM."""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib import error

# The official Windows embeddable Python runtime uses isolated search paths and
# does not automatically add the launched script's directory to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from collector import COLLECTOR_VERSION, CollectorRestart, DEFAULT_LOG_BACKUPS, DEFAULT_LOG_MAX_BYTES, append_log, attach_account_usage, attribution_scope, process_server_response, read_account, read_alert_receipts, read_sessions, send, track_target_usage, collector_configuration, configure_collector, monitoring_configuration, registered_snapshots


SKIP_PROFILES = {"all users", "default", "default user", "public", "defaultapppool", "wdagutilityaccount"}


def profiles_root_for_drive(system_drive: str) -> Path:
    drive = (system_drive or "C:").rstrip("\\/")
    return Path(drive + "\\Users")


def discover_profiles(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    profiles = []
    for candidate in root.iterdir():
        if not candidate.is_dir() or candidate.name.lower() in SKIP_PROFILES:
            continue
        codex_dir = candidate / ".codex"
        # A fresh Codex login can create auth.json before the first session DB.
        # Register the account node immediately; sessions will appear later.
        if (codex_dir / "auth.json").is_file() or (codex_dir / "state_5.sqlite").is_file():
            profiles.append(candidate)
    return sorted(profiles, key=lambda item: item.name.lower())


def profile_snapshot(profile: Path, token: str, include_rollout_path: bool = False) -> dict:
    hostname, os_user = socket.gethostname(), profile.name
    codex_dir = profile / ".codex"
    state_db = codex_dir / "state_5.sqlite"
    return {
        "version": 1,
        "collector": {"version": COLLECTOR_VERSION, "kind": "windows-machine"},
        "node": {
            "key": hashlib.sha256(f"{hostname}:{os_user}".encode()).hexdigest()[:12],
            "hostname": hostname,
            "os_user": os_user,
        },
        "account": read_account(codex_dir / "auth.json", token),
        "sessions": (
            read_sessions(state_db, token, include_rollout_path=include_rollout_path)
            if state_db.is_file() else []
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--profiles-root", type=Path, default=profiles_root_for_drive(os.environ.get("SystemDrive", "C:")))
    parser.add_argument("--account-key", help="Legacy account binding for attribution migration")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--log-max-bytes", type=int, default=DEFAULT_LOG_MAX_BYTES)
    parser.add_argument("--log-backups", type=int, default=DEFAULT_LOG_BACKUPS)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--config-file", type=Path)
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--show-config", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        if configure_collector(args):
            return
        collector_configuration(args)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    state_file = args.state_file or args.token_file.with_name("collector-state.json")

    def log(message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        if sys.stdout:
            print(line, flush=True)
        if args.log_file:
            append_log(args.log_file, line, max(0, args.log_max_bytes), max(0, args.log_backups))

    while True:
        try:
            _, settings = collector_configuration(args)
            token = Path(settings["token_file"]).read_text().strip()
            if not token:
                raise ValueError("collector token is empty")
        except (OSError, ValueError) as exc:
            log(f"configuration error: {exc}")
            if args.once:
                raise SystemExit(1) from exc
            time.sleep(max(10, args.interval))
            continue
        profiles = discover_profiles(args.profiles_root)
        sent, failures = 0, 0
        for profile in profiles:
            try:
                payload = profile_snapshot(profile, token, include_rollout_path=True)
                payload["alert_receipts"] = read_alert_receipts(state_file)
                configuration = monitoring_configuration(settings["server"], payload, token, settings["account_key"])
                process_server_response(configuration, state_file, log, windows_user=profile.name)
                reports = registered_snapshots(payload, configuration, profile / ".codex" / "auth.json",
                                               state_file, token, settings["account_key"])
                for report in reports:
                    usage_source = "local-fallback"
                    if report["account"]["key"] == payload["account"].get("key"):
                        try:
                            attach_account_usage(report, profile / ".codex")
                            usage_source = "codex-app-server"
                        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                            log(f"profile={profile.name} account-usage-fallback={exc}")
                    response = send(settings["server"], report, token)
                    log(f"profile={profile.name} account={report['account']['key']} sent source={usage_source}")
                    sent += 1
                    process_server_response(response, state_file, log, windows_user=profile.name,
                                            script_dir=Path(__file__).resolve().parent)
                if not reports:
                    log(f"profile={profile.name} discovered; waiting for monitoring registration")
            except CollectorRestart:
                os.execv(sys.executable, [sys.executable, *sys.argv])
            except (OSError, ValueError, sqlite3.Error, error.URLError, RuntimeError) as exc:
                failures += 1
                log(f"profile={profile.name} error={exc}")
        log(f"scan profiles={len(profiles)} sent={sent} failures={failures}")
        if args.once:
            if failures:
                raise SystemExit(1)
            return
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    main()
