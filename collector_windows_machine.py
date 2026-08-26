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

from collector import COLLECTOR_VERSION, CollectorRestart, DEFAULT_LOG_BACKUPS, DEFAULT_LOG_MAX_BYTES, append_log, attach_account_usage, attribution_scope, process_server_response, read_account, read_alert_receipts, read_sessions, send, track_target_usage


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
    parser.add_argument("--server", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--profiles-root", type=Path, default=profiles_root_for_drive(os.environ.get("SystemDrive", "C:")))
    parser.add_argument("--account-key", required=True, help="Only send this anonymized Codex account")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--log-max-bytes", type=int, default=DEFAULT_LOG_MAX_BYTES)
    parser.add_argument("--log-backups", type=int, default=DEFAULT_LOG_BACKUPS)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = args.token_file.read_text().strip()
    if not token:
        raise SystemExit("collector token is empty")

    state_file = args.state_file or args.token_file.with_name("collector-state.json")

    def log(message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        if sys.stdout:
            print(line, flush=True)
        if args.log_file:
            append_log(args.log_file, line, max(0, args.log_max_bytes), max(0, args.log_backups))

    while True:
        profiles = discover_profiles(args.profiles_root)
        sent, failures = 0, 0
        for profile in profiles:
            try:
                payload = profile_snapshot(profile, token, include_rollout_path=True)
                payload["alert_receipts"] = read_alert_receipts(state_file)
                payload["sessions"] = track_target_usage(
                    payload["sessions"], payload["account"].get("key") or "unknown",
                    args.account_key,
                    profile / ".codex" / "auth.json", state_file,
                    attribution_scope(payload["node"]["key"], token),
                )
                if payload["account"].get("key") != args.account_key:
                    log(f"profile={profile.name} skipped=unregistered-account")
                    continue
                usage_source = "local-fallback"
                try:
                    attach_account_usage(payload, profile / ".codex")
                    usage_source = "codex-app-server"
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    log(f"profile={profile.name} account-usage-fallback={exc}")
                response = send(args.server, payload, token)
                log(f"profile={profile.name} sent source={usage_source}")
                sent += 1
                process_server_response(
                    response, state_file, log, windows_user=profile.name,
                    script_dir=Path(__file__).resolve().parent,
                )
            except CollectorRestart:
                os.execv(sys.executable, [sys.executable, *sys.argv])
            except (OSError, sqlite3.Error, error.URLError, RuntimeError) as exc:
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
