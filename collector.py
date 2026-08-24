#!/usr/bin/env python3
"""Read one user's local Codex metadata and send a sanitized snapshot."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import socket
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request


def decode_jwt(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


def digest(kind: str, value: str, token: str) -> str:
    return hashlib.sha256(f"{token}:{kind}:{value}".encode()).hexdigest()[:12]


def read_account(auth_file: Path, token: str) -> dict[str, Any]:
    try:
        auth = json.loads(auth_file.read_text())
        tokens = auth.get("tokens") or {}
        claims = decode_jwt(tokens.get("id_token") or "")
        details = claims.get("https://api.openai.com/auth") or {}
        account_id = tokens.get("account_id") or details.get("chatgpt_account_id") or claims.get("sub") or "unknown"
        return {
            "key": digest("account", str(account_id), token),
            "name": claims.get("name") or "Unknown",
            "email": claims.get("email"),
            "email_verified": bool(claims.get("email_verified")),
            "plan": details.get("chatgpt_plan_type"),
            "auth_provider": claims.get("auth_provider"),
            "auth_mode": auth.get("auth_mode"),
        }
    except (OSError, json.JSONDecodeError):
        return {"key": "unknown", "name": "Unknown", "email": None, "plan": None}


def read_sessions(state_db: Path, token: str) -> list[dict[str, Any]]:
    if not state_db.is_file():
        raise FileNotFoundError(f"Codex database not found: {state_db}")
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        # Never select titles, previews, messages, rollouts, or full paths for transmission.
        rows = connection.execute(
            "SELECT id, cwd, model, tokens_used, created_at, updated_at, archived FROM threads"
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "key": digest("session", row["id"], token),
            "project_key": digest("project", row["cwd"], token),
            "project_name": Path(row["cwd"]).name or "unknown",
            "model": row["model"] or "unknown",
            "tokens": max(0, int(row["tokens_used"] or 0)),
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
            "archived": bool(row["archived"]),
        }
        for row in rows
    ]


def snapshot(state_db: Path, auth_file: Path, token: str) -> dict[str, Any]:
    hostname, os_user = socket.gethostname(), getpass.getuser()
    return {
        "version": 1,
        "node": {
            "key": hashlib.sha256(f"{hostname}:{os_user}".encode()).hexdigest()[:12],
            "hostname": hostname,
            "os_user": os_user,
        },
        "account": read_account(auth_file, token),
        "sessions": read_sessions(state_db, token),
    }


def send(server: str, payload: dict[str, Any], token: str) -> None:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(token.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    endpoint = server.rstrip("/") + "/api/ingest"
    req = request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Codex-Timestamp": timestamp,
            "X-Codex-Signature": signature,
        },
    )
    with request.urlopen(req, timeout=15) as response:
        if response.status != 202:
            raise RuntimeError(f"collector rejected: HTTP {response.status}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=os.getenv("CODEX_MONITOR_SERVER"), required=not os.getenv("CODEX_MONITOR_SERVER"))
    parser.add_argument("--token-file", type=Path, default=Path(os.getenv("CODEX_COLLECTOR_TOKEN_FILE", "~/.config/codex-usage-collector.token")).expanduser())
    parser.add_argument("--state-db", type=Path, default=Path("~/.codex/state_5.sqlite").expanduser())
    parser.add_argument("--auth-file", type=Path, default=Path("~/.codex/auth.json").expanduser())
    parser.add_argument("--account-key", default=os.getenv("CODEX_TARGET_ACCOUNT_KEY"), help="Only send this anonymized Codex account")
    parser.add_argument("--interval", type=int, default=int(os.getenv("CODEX_COLLECTOR_INTERVAL", "30")))
    parser.add_argument("--log-file", type=Path, help="Optional log file (useful for Windows scheduled tasks)")
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
        try:
            payload = snapshot(args.state_db, args.auth_file, token)
            if args.account_key and payload["account"].get("key") != args.account_key:
                log(f"skipped unregistered account on {payload['node']['hostname']}:{payload['node']['os_user']}")
            else:
                send(args.server, payload, token)
                log(f"sent {len(payload['sessions'])} sessions from {payload['node']['hostname']}:{payload['node']['os_user']}")
        except (OSError, sqlite3.Error, error.URLError, RuntimeError) as exc:
            log(f"collector error: {exc}")
            if args.once:
                raise SystemExit(1) from exc
        if args.once:
            return
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    main()
