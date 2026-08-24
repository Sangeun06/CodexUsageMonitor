#!/usr/bin/env python3
"""Machine-wide Windows collector intended to run as SYSTEM."""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import sqlite3
import sys
import time
from pathlib import Path
from urllib import error

# The official Windows embeddable Python runtime uses isolated search paths and
# does not automatically add the launched script's directory to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from collector import read_account, read_sessions, send


SKIP_PROFILES = {"all users", "default", "default user", "public", "defaultapppool", "wdagutilityaccount"}


def discover_profiles(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    profiles = []
    for candidate in root.iterdir():
        if not candidate.is_dir() or candidate.name.lower() in SKIP_PROFILES:
            continue
        if (candidate / ".codex" / "state_5.sqlite").is_file():
            profiles.append(candidate)
    return sorted(profiles, key=lambda item: item.name.lower())


def profile_snapshot(profile: Path, token: str) -> dict:
    hostname, os_user = socket.gethostname(), profile.name
    codex_dir = profile / ".codex"
    return {
        "version": 1,
        "node": {
            "key": hashlib.sha256(f"{hostname}:{os_user}".encode()).hexdigest()[:12],
            "hostname": hostname,
            "os_user": os_user,
        },
        "account": read_account(codex_dir / "auth.json", token),
        "sessions": read_sessions(codex_dir / "state_5.sqlite", token),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--profiles-root", type=Path, default=Path(os.environ.get("SystemDrive", "C:")) / "Users")
    parser.add_argument("--account-key", required=True, help="Only send this anonymized Codex account")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = args.token_file.read_text().strip()
    if not token:
        raise SystemExit("collector token is empty")

    def log(message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        if sys.stdout:
            print(line, flush=True)
        if args.log_file:
            args.log_file.parent.mkdir(parents=True, exist_ok=True)
            with args.log_file.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    while True:
        profiles = discover_profiles(args.profiles_root)
        sent, failures = 0, 0
        for profile in profiles:
            try:
                payload = profile_snapshot(profile, token)
                if payload["account"].get("key") != args.account_key:
                    log(f"profile={profile.name} skipped=unregistered-account")
                    continue
                send(args.server, payload, token)
                sent += 1
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
