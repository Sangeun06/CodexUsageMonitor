#!/usr/bin/env python3
"""Privacy-first local Codex token usage dashboard."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from collector import COLLECTOR_VERSION, read_app_server_usage


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DEFAULT_SOURCE_DB = Path.home() / ".codex" / "state_5.sqlite"
DEFAULT_MONITOR_DB = APP_DIR / "data" / "monitor.sqlite"
DEFAULT_AUTH_FILE = Path.home() / ".codex" / "auth.json"
DEFAULT_COLLECTOR_TOKEN = APP_DIR / "data" / "collector.token"
DEFAULT_DASHBOARD_PASSWORD = APP_DIR / "data" / "dashboard.password"


def utc_iso(timestamp: int | float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def anonymous_id(kind: str, value: str, salt: str) -> str:
    digest = hashlib.sha256(f"{salt}:{kind}:{value}".encode()).hexdigest()
    return digest[:12]


@dataclass(frozen=True)
class Config:
    source_db: Path
    monitor_db: Path
    salt: str
    auth_file: Path = DEFAULT_AUTH_FILE
    collector_token_file: Path = DEFAULT_COLLECTOR_TOKEN
    dashboard_password_file: Path = DEFAULT_DASHBOARD_PASSWORD
    interval: int = 10
    active_window: int = 300


class UsageStore:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        config.monitor_db.parent.mkdir(parents=True, exist_ok=True)
        self.collector_token = self._load_or_create_collector_token()
        self.dashboard_user = os.getenv("CODEX_DASHBOARD_USER", "admin")
        self.dashboard_password = self._load_or_create_dashboard_password()
        self._initialize_monitor_db()

    def _load_or_create_collector_token(self) -> str:
        env_token = os.getenv("CODEX_COLLECTOR_TOKEN")
        if env_token:
            return env_token
        path = self.config.collector_token_file
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return path.read_text().strip()
        token = secrets.token_urlsafe(32)
        path.write_text(token + "\n")
        path.chmod(0o600)
        return token

    def _load_or_create_dashboard_password(self) -> str:
        env_password = os.getenv("CODEX_DASHBOARD_PASSWORD")
        if env_password:
            return env_password
        path = self.config.dashboard_password_file
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return path.read_text().strip()
        password = secrets.token_urlsafe(18)
        path.write_text(password + "\n")
        path.chmod(0o600)
        return password

    @contextmanager
    def _connect_monitor(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.config.monitor_db, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _connect_source(self) -> Iterator[sqlite3.Connection]:
        if not self.config.source_db.is_file():
            raise FileNotFoundError(f"Codex database not found: {self.config.source_db}")
        uri = f"file:{self.config.source_db}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_monitor_db(self) -> None:
        with self._connect_monitor() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS overview_samples (
                    observed_at INTEGER PRIMARY KEY,
                    total_tokens INTEGER NOT NULL,
                    session_count INTEGER NOT NULL,
                    active_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_samples (
                    observed_at INTEGER NOT NULL,
                    session_key TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    model TEXT NOT NULL,
                    tokens_used INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    archived INTEGER NOT NULL,
                    PRIMARY KEY (observed_at, session_key)
                );
                CREATE INDEX IF NOT EXISTS idx_session_samples_key_time
                    ON session_samples(session_key, observed_at DESC);
                CREATE TABLE IF NOT EXISTS collector_nodes (
                    node_key TEXT PRIMARY KEY,
                    hostname TEXT NOT NULL,
                    os_user TEXT NOT NULL,
                    account_json TEXT NOT NULL,
                    seen_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collector_sessions (
                    node_key TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    tokens_used INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    archived INTEGER NOT NULL,
                    PRIMARY KEY (node_key, session_key),
                    FOREIGN KEY (node_key) REFERENCES collector_nodes(node_key) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS node_aliases (
                    node_key TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_usage (
                    account_key TEXT PRIMARY KEY,
                    usage_json TEXT NOT NULL,
                    reported_by_node_key TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    received_at INTEGER NOT NULL,
                    lifetime_tokens INTEGER
                );
                CREATE TABLE IF NOT EXISTS account_limit_samples (
                    account_key TEXT NOT NULL,
                    limit_id TEXT NOT NULL,
                    window_kind TEXT NOT NULL,
                    sampled_at INTEGER NOT NULL,
                    window_minutes INTEGER NOT NULL,
                    used_percent REAL NOT NULL,
                    resets_at INTEGER NOT NULL,
                    PRIMARY KEY (account_key, limit_id, window_kind, sampled_at)
                );
                CREATE INDEX IF NOT EXISTS idx_account_limit_window
                    ON account_limit_samples(account_key, resets_at, sampled_at);
                CREATE TABLE IF NOT EXISTS account_token_samples (
                    account_key TEXT NOT NULL,
                    sampled_at INTEGER NOT NULL,
                    lifetime_tokens INTEGER NOT NULL,
                    PRIMARY KEY (account_key, sampled_at)
                );
                CREATE INDEX IF NOT EXISTS idx_account_token_time
                    ON account_token_samples(account_key, sampled_at);
                CREATE TABLE IF NOT EXISTS collector_alerts (
                    alert_key TEXT PRIMARY KEY,
                    node_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    delivered_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_collector_alert_delivery
                    ON collector_alerts(node_key, delivered_at, created_at);
                CREATE TABLE IF NOT EXISTS collector_session_usage (
                    node_key TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    bucket_at INTEGER NOT NULL,
                    tokens_used INTEGER NOT NULL,
                    PRIMARY KEY (node_key, session_key, bucket_at)
                );
                CREATE INDEX IF NOT EXISTS idx_collector_session_usage_time
                    ON collector_session_usage(node_key, bucket_at);
                """
            )

    @staticmethod
    def _version_tuple(value: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in value.split("."))
        except ValueError:
            return ()

    def _collector_update(self, collector_info: Any) -> dict[str, Any] | None:
        if not isinstance(collector_info, dict):
            return None
        installed = str(collector_info.get("version") or "")
        installed_version = self._version_tuple(installed)
        if (
            not installed_version
            or installed_version < (0, 6, 0)
            or installed_version >= self._version_tuple(COLLECTOR_VERSION)
        ):
            return None
        kind = str(collector_info.get("kind") or "user")
        names = ["collector.py"]
        if kind == "windows-machine":
            names.append("collector_windows_machine.py")
        files = {}
        for name in names:
            content = (APP_DIR / name).read_bytes()
            files[name] = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "content": base64.b64encode(content).decode(),
            }
        return {"version": COLLECTOR_VERSION, "files": files}

    def _collector_alerts(self, node_key: str, now: int) -> list[dict[str, Any]]:
        account_usage = self.read_account_usage()
        weekly = self.read_weekly_usage_history(account_usage, now)
        if not weekly:
            return []
        pace = weekly.get("pace") or {}
        used = float(weekly.get("used_percent") or 0)
        recommended = float(pace.get("elapsed_percent") or 0)
        if recommended <= 0 or used < recommended * 1.5:
            return []
        collector_nodes, collector_sessions = self.read_remote(include_local=True)
        if any(node["key"] == self.local_node_key() for node in collector_nodes):
            nodes, all_sessions = collector_nodes, collector_sessions
        else:
            local_node = {
                "key": self.local_node_key(), "hostname": socket.gethostname(),
                "os_user": getpass.getuser(), "seen_at": now, "account": self.read_account(),
            }
            nodes = [local_node] + collector_nodes
            all_sessions = self.read_sessions() + collector_sessions
        aliases = self.read_aliases()
        for node in nodes:
            node["display_name"] = aliases.get(node["key"], node["os_user"])
        reconciliation = self.read_usage_reconciliation(all_sessions, nodes, account_usage, weekly, now)
        comparison = (reconciliation or {}).get("node_comparison") or []
        if not comparison:
            return []
        target = max(comparison, key=lambda item: int(item.get("tokens") or 0))
        if int(target.get("tokens") or 0) <= 0:
            return []
        resets_at = str(weekly.get("resets_at") or "unknown")
        band = max(0, min(100, int(used // 10) * 10))
        alert_key = hashlib.sha256(
            f"weekly-pace-150:{resets_at}:{band}:{target['node_key']}".encode()
        ).hexdigest()[:24]
        share = float(target.get("share_percent") or 0)
        payload = {
            "id": alert_key,
            "kind": "weekly-pace-150",
            "severity": "warning",
            "title": "Codex 주간 사용 속도 경고",
            "message": (
                f"공유 계정 사용률 {used:.1f}%가 현재 권장선 {recommended:.1f}%의 "
                f"150%를 넘었습니다. 이 장비의 확인된 주간 사용 비중은 {share:.1f}%입니다."
            ),
            "used_percent": used,
            "recommended_percent": recommended,
            "resets_at": resets_at,
        }
        with self._lock, self._connect_monitor() as db:
            db.execute(
                """INSERT OR IGNORE INTO collector_alerts
                   (alert_key, node_key, payload_json, created_at, delivered_at)
                   VALUES (?, ?, ?, ?, NULL)""",
                (alert_key, target["node_key"], json.dumps(payload, separators=(",", ":")), now),
            )
            if node_key != target["node_key"]:
                return []
            rows = db.execute(
                """SELECT alert_key, payload_json FROM collector_alerts
                   WHERE node_key = ? AND delivered_at IS NULL ORDER BY created_at LIMIT 8""",
                (node_key,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def collector_response(
        self, node_key: str, collector_info: Any, now: int | None = None,
    ) -> dict[str, Any]:
        now = int(now or time.time())
        response: dict[str, Any] = {"ok": True, "server_version": COLLECTOR_VERSION}
        update = self._collector_update(collector_info)
        if update:
            response["update"] = update
        alerts = self._collector_alerts(node_key, now)
        if alerts:
            response["alerts"] = alerts
        return response

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict[str, Any]:
        try:
            part = token.split(".")[1]
            part += "=" * (-len(part) % 4)
            return json.loads(base64.urlsafe_b64decode(part))
        except (IndexError, ValueError, json.JSONDecodeError):
            return {}

    def read_account(self, identity_salt: str | None = None) -> dict[str, Any]:
        empty = {"key": "unknown", "name": "Unknown", "email": None, "plan": None, "auth_provider": None, "auth_mode": None}
        try:
            auth = json.loads(self.config.auth_file.read_text())
            tokens = auth.get("tokens") or {}
            claims = self._decode_jwt_payload(tokens.get("id_token") or "")
            account_claims = claims.get("https://api.openai.com/auth") or {}
            account_id = tokens.get("account_id") or account_claims.get("chatgpt_account_id") or claims.get("sub") or "unknown"
            return {
                "key": anonymous_id("account", str(account_id), identity_salt or self.config.salt),
                "name": claims.get("name") or "Unknown",
                "email": claims.get("email"),
                "email_verified": bool(claims.get("email_verified")),
                "plan": account_claims.get("chatgpt_plan_type"),
                "auth_provider": claims.get("auth_provider"),
                "auth_mode": auth.get("auth_mode"),
            }
        except (OSError, json.JSONDecodeError):
            return empty

    def collector_account_key(self) -> str:
        return self.read_account(self.collector_token)["key"]

    def local_node_key(self) -> str:
        return hashlib.sha256(f"{socket.gethostname()}:{getpass.getuser()}".encode()).hexdigest()[:12]

    def read_sessions(self) -> list[dict[str, Any]]:
        # Deliberately excludes title, preview, messages, rollout_path, and cwd.
        query = """
            SELECT id, cwd, model, tokens_used, created_at, updated_at, archived
            FROM threads
            ORDER BY updated_at DESC
        """
        with self._connect_source() as db:
            rows = db.execute(query).fetchall()
        return [
            {
                "key": anonymous_id("session", row["id"], self.config.salt),
                "project_key": anonymous_id("project", row["cwd"], self.config.salt),
                "project_name": Path(row["cwd"]).name or "unknown",
                "model": row["model"] or "unknown",
                "tokens": max(0, int(row["tokens_used"] or 0)),
                "created_at": int(row["created_at"]),
                "updated_at": int(row["updated_at"]),
                "archived": bool(row["archived"]),
                "node_key": self.local_node_key(),
                "hostname": socket.gethostname(),
                "os_user": getpass.getuser(),
                "source": "local",
            }
            for row in rows
        ]

    def ingest(self, payload: dict[str, Any], now: int | None = None) -> None:
        now = int(now or time.time())
        node = payload.get("node") or {}
        account = payload.get("account") or {}
        sessions = payload.get("sessions") or []
        required = ("key", "hostname", "os_user")
        if not all(isinstance(node.get(field), str) and node[field] for field in required):
            raise ValueError("invalid collector node")
        if not isinstance(sessions, list) or len(sessions) > 10_000:
            raise ValueError("invalid sessions")
        clean_account = {
            field: account.get(field)
            for field in ("key", "name", "email", "email_verified", "plan", "auth_provider", "auth_mode")
        }
        target_account_key = self.collector_account_key()
        if target_account_key != "unknown" and clean_account.get("key") != target_account_key:
            raise ValueError("account is not registered for this monitor")
        raw_receipts = payload.get("alert_receipts") or []
        if not isinstance(raw_receipts, list) or len(raw_receipts) > 200:
            raise ValueError("invalid alert receipts")
        alert_receipts = [
            item for item in raw_receipts
            if isinstance(item, str) and re.fullmatch(r"[a-f0-9]{24}", item)
        ]
        clean_sessions = []
        clean_usage_buckets = []
        total_buckets = 0
        for session in sessions:
            if not isinstance(session, dict) or not session.get("key"):
                raise ValueError("invalid session")
            clean_sessions.append(
                (
                    node["key"], str(session["key"])[:64], str(session.get("project_key") or "unknown")[:64],
                    str(session.get("project_name") or "unknown")[:120], str(session.get("model") or "unknown")[:120],
                    max(0, int(session.get("tokens") or 0)), int(session.get("created_at") or now),
                    int(session.get("updated_at") or now), int(bool(session.get("archived"))),
                )
            )
            raw_buckets = session.get("usage_buckets")
            if raw_buckets is not None and not isinstance(raw_buckets, list):
                raise ValueError("invalid session usage buckets")
            session_key = str(session["key"])[:64]
            for bucket in raw_buckets or []:
                if not isinstance(bucket, dict):
                    raise ValueError("invalid session usage bucket")
                bucket_at = self._nonnegative_int(bucket.get("at"))
                bucket_tokens = self._nonnegative_int(bucket.get("tokens"))
                if bucket_at is None or bucket_tokens is None:
                    raise ValueError("invalid session usage bucket")
                clean_usage_buckets.append((node["key"][:64], session_key, bucket_at, bucket_tokens))
                total_buckets += 1
                if total_buckets > 50_000:
                    raise ValueError("too many session usage buckets")
        with self._lock, self._connect_monitor() as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(
                """INSERT INTO collector_nodes (node_key, hostname, os_user, account_json, seen_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(node_key) DO UPDATE SET hostname=excluded.hostname,
                   os_user=excluded.os_user, account_json=excluded.account_json, seen_at=excluded.seen_at""",
                (node["key"][:64], node["hostname"][:120], node["os_user"][:120], json.dumps(clean_account), now),
            )
            db.execute("DELETE FROM collector_sessions WHERE node_key = ?", (node["key"],))
            db.executemany(
                """INSERT INTO collector_sessions
                   (node_key, session_key, project_key, project_name, model, tokens_used, created_at, updated_at, archived)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                clean_sessions,
            )
            db.executemany(
                """INSERT INTO collector_session_usage
                   (node_key, session_key, bucket_at, tokens_used) VALUES (?, ?, ?, ?)
                   ON CONFLICT(node_key, session_key, bucket_at) DO UPDATE SET
                   tokens_used=MAX(tokens_used, excluded.tokens_used)""",
                clean_usage_buckets,
            )
            db.execute(
                "DELETE FROM collector_session_usage WHERE bucket_at < ?",
                (now - 14 * 86400,),
            )
            if alert_receipts:
                db.executemany(
                    """UPDATE collector_alerts SET delivered_at = ?
                       WHERE alert_key = ? AND node_key = ?""",
                    [(now, alert_key, node["key"][:64]) for alert_key in alert_receipts],
                )
            if isinstance(payload.get("account_usage"), dict):
                self._store_account_usage(
                    db, str(clean_account.get("key") or "unknown"), node["key"][:64],
                    payload["account_usage"], now,
                )

    @staticmethod
    def _nonnegative_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @classmethod
    def _clean_account_usage(cls, usage: dict[str, Any], now: int) -> dict[str, Any]:
        if usage.get("source") != "codex-app-server":
            raise ValueError("invalid account usage source")
        raw_summary = usage.get("summary")
        if not isinstance(raw_summary, dict):
            raise ValueError("invalid account usage summary")
        summary_fields = (
            "lifetime_tokens", "peak_daily_tokens", "longest_running_turn_seconds",
            "current_streak_days", "longest_streak_days",
        )
        summary = {field: cls._nonnegative_int(raw_summary.get(field)) for field in summary_fields}

        daily_usage = []
        raw_daily = usage.get("daily_usage")
        if raw_daily is not None and not isinstance(raw_daily, list):
            raise ValueError("invalid daily account usage")
        for bucket in (raw_daily or [])[-400:]:
            if not isinstance(bucket, dict) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(bucket.get("date") or "")):
                continue
            tokens = cls._nonnegative_int(bucket.get("tokens"))
            if tokens is not None:
                daily_usage.append({"date": bucket["date"], "tokens": tokens})
        daily_usage.sort(key=lambda bucket: bucket["date"])

        def clean_window(window: Any) -> dict[str, Any] | None:
            if not isinstance(window, dict):
                return None
            try:
                used = min(100.0, max(0.0, float(window.get("used_percent"))))
            except (TypeError, ValueError):
                return None
            return {
                "used_percent": used,
                "window_minutes": cls._nonnegative_int(window.get("window_minutes")),
                "resets_at": cls._nonnegative_int(window.get("resets_at")),
            }

        rate_limits = []
        raw_limits = usage.get("rate_limits")
        if raw_limits is not None and not isinstance(raw_limits, list):
            raise ValueError("invalid account rate limits")
        for limit in (raw_limits or [])[:16]:
            if not isinstance(limit, dict):
                continue
            primary, secondary = clean_window(limit.get("primary")), clean_window(limit.get("secondary"))
            if primary is None and secondary is None:
                continue
            rate_limits.append({
                "id": str(limit.get("id") or "codex")[:80],
                "name": str(limit.get("name") or limit.get("id") or "Codex")[:120],
                "plan": str(limit.get("plan"))[:80] if limit.get("plan") else None,
                "primary": primary,
                "secondary": secondary,
            })
        observed_at = cls._nonnegative_int(usage.get("observed_at")) or now
        return {
            "source": "codex-app-server", "observed_at": observed_at,
            "summary": summary, "daily_usage": daily_usage, "rate_limits": rate_limits,
        }

    def _store_account_usage(
        self, db: sqlite3.Connection, account_key: str, node_key: str,
        raw_usage: dict[str, Any], received_at: int,
    ) -> None:
        usage = self._clean_account_usage(raw_usage, received_at)
        sample_at = received_at - received_at % 30
        for limit in usage["rate_limits"]:
            for window_kind in ("primary", "secondary"):
                window = limit.get(window_kind)
                if not window or window.get("window_minutes") is None or window.get("resets_at") is None:
                    continue
                db.execute(
                    """INSERT OR REPLACE INTO account_limit_samples
                       (account_key, limit_id, window_kind, sampled_at, window_minutes, used_percent, resets_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (account_key, limit["id"], window_kind, sample_at, window["window_minutes"],
                     window["used_percent"], window["resets_at"]),
                )
        db.execute("DELETE FROM account_limit_samples WHERE sampled_at < ?", (received_at - 14 * 86400,))
        lifetime = usage["summary"]["lifetime_tokens"]
        current = db.execute(
            "SELECT lifetime_tokens FROM account_usage WHERE account_key = ?", (account_key,)
        ).fetchone()
        # Multiple machines report the same account-wide counter. Keep one
        # canonical snapshot and reject a delayed report that would regress it.
        if current is not None and current["lifetime_tokens"] is not None:
            if lifetime is None or lifetime < current["lifetime_tokens"]:
                return
        if lifetime is not None:
            db.execute(
                """INSERT OR REPLACE INTO account_token_samples
                   (account_key, sampled_at, lifetime_tokens) VALUES (?, ?, ?)""",
                (account_key, sample_at, lifetime),
            )
            db.execute(
                "DELETE FROM account_token_samples WHERE sampled_at < ?",
                (received_at - 14 * 86400,),
            )
        db.execute(
            """INSERT INTO account_usage
               (account_key, usage_json, reported_by_node_key, observed_at, received_at, lifetime_tokens)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_key) DO UPDATE SET usage_json=excluded.usage_json,
               reported_by_node_key=excluded.reported_by_node_key, observed_at=excluded.observed_at,
               received_at=excluded.received_at, lifetime_tokens=excluded.lifetime_tokens""",
            (account_key, json.dumps(usage, separators=(",", ":")), node_key,
             usage["observed_at"], received_at, lifetime),
        )

    def store_local_account_usage(self, usage: dict[str, Any], now: int | None = None) -> None:
        now = int(now or time.time())
        account_key = self.collector_account_key()
        if account_key == "unknown":
            raise RuntimeError("local Codex account is not registered")
        with self._lock, self._connect_monitor() as db:
            self._store_account_usage(db, account_key, self.local_node_key(), usage, now)

    def read_account_usage(self) -> dict[str, Any] | None:
        account_key = self.collector_account_key()
        with self._lock, self._connect_monitor() as db:
            if account_key == "unknown":
                row = db.execute(
                    "SELECT * FROM account_usage ORDER BY received_at DESC LIMIT 1"
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM account_usage WHERE account_key = ?", (account_key,)
                ).fetchone()
        if row is None:
            return None
        usage = json.loads(row["usage_json"])
        usage.update({
            "account_key": row["account_key"], "reported_by_node_key": row["reported_by_node_key"],
            "observed_at": row["observed_at"], "received_at": row["received_at"],
        })
        return usage

    @staticmethod
    def _select_weekly_window(usage: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
        candidates = []
        for limit in usage.get("rate_limits", []):
            for window_kind in ("primary", "secondary"):
                window = limit.get(window_kind)
                if not window or window.get("window_minutes") != 10_080 or window.get("resets_at") is None:
                    continue
                candidates.append((
                    0 if limit.get("id") == "codex" else 1,
                    0 if window_kind == "primary" else 1,
                    str(limit.get("id") or "codex"), window_kind, window,
                ))
        if not candidates:
            return None
        _, _, limit_id, window_kind, window = min(candidates, key=lambda item: item[:2])
        return limit_id, window_kind, window

    def read_weekly_usage_history(
        self, account_usage: dict[str, Any] | None, now: int | None = None,
    ) -> dict[str, Any] | None:
        if not account_usage:
            return None
        selected = self._select_weekly_window(account_usage)
        if selected is None:
            return None
        limit_id, window_kind, window = selected
        now = int(now or time.time())
        resets_at = int(window["resets_at"])
        window_start = resets_at - int(window["window_minutes"]) * 60
        with self._lock, self._connect_monitor() as db:
            rows = db.execute(
                """SELECT sampled_at, used_percent FROM account_limit_samples
                   WHERE account_key = ? AND limit_id = ? AND window_kind = ?
                   AND resets_at = ? AND sampled_at >= ? ORDER BY sampled_at""",
                (account_usage["account_key"], limit_id, window_kind, resets_at, window_start),
            ).fetchall()
        window_seconds = int(window["window_minutes"]) * 60
        points = [{"at": utc_iso(window_start), "used_percent": 0.0, "ideal_percent": 0.0}]
        points.extend({
            "at": utc_iso(row["sampled_at"]),
            "used_percent": row["used_percent"],
            "ideal_percent": min(100.0, max(0.0, (row["sampled_at"] - window_start) / window_seconds * 100)),
        } for row in rows)
        elapsed_seconds = min(window_seconds, max(0, now - window_start))
        elapsed_percent = elapsed_seconds / window_seconds * 100
        used_percent = float(window["used_percent"])
        deviation = used_percent - elapsed_percent
        tolerance = 100 / 14
        if deviation > tolerance:
            pace_status, pace_label = "fast", "권장 속도보다 빠름"
        elif deviation < -tolerance:
            pace_status, pace_label = "saving", "권장 속도보다 여유"
        else:
            pace_status, pace_label = "on_track", "권장 범위"
        projected_percent = 0.0
        projected_exhaustion_at = None
        if elapsed_seconds > 0 and used_percent > 0:
            projected_percent = min(999.0, used_percent / elapsed_seconds * window_seconds)
            exhaustion_at = window_start + int(elapsed_seconds / used_percent * 100)
            if exhaustion_at < resets_at:
                projected_exhaustion_at = utc_iso(exhaustion_at)
        remaining_days = max(0.0, (resets_at - now) / 86400)
        return {
            "limit_id": limit_id,
            "window_kind": window_kind,
            "window_minutes": window["window_minutes"],
            "window_started_at": utc_iso(window_start),
            "resets_at": utc_iso(resets_at),
            "used_percent": used_percent,
            "points": points,
            "pace": {
                "status": pace_status,
                "label": pace_label,
                "elapsed_percent": elapsed_percent,
                "deviation_percent": deviation,
                "daily_budget_percent": 100 / 7,
                "remaining_daily_budget_percent": (
                    max(0.0, 100 - used_percent) / remaining_days if remaining_days > 0 else 0.0
                ),
                "projected_end_percent": projected_percent,
                "projected_exhaustion_at": projected_exhaustion_at,
            },
        }

    def read_usage_reconciliation(
        self,
        all_sessions: list[dict[str, Any]],
        nodes: list[dict[str, Any]],
        account_usage: dict[str, Any] | None,
        weekly_usage: dict[str, Any] | None,
        now: int,
    ) -> dict[str, Any] | None:
        if not account_usage:
            return None
        lifetime = account_usage.get("summary", {}).get("lifetime_tokens")
        if lifetime is None:
            return None
        attributed_total = sum(max(0, int(session.get("tokens") or 0)) for session in all_sessions)
        lifetime_difference = int(lifetime) - attributed_total
        account_key = account_usage["account_key"]
        requested_start = int(datetime.fromisoformat(
            weekly_usage["window_started_at"].replace("Z", "+00:00")
        ).timestamp()) if weekly_usage else now - 7 * 86400

        with self._lock, self._connect_monitor() as db:
            account_rows = db.execute(
                """SELECT sampled_at, lifetime_tokens FROM account_token_samples
                   WHERE account_key = ? AND sampled_at >= ? ORDER BY sampled_at""",
                (account_key, requested_start),
            ).fetchall()
            baselines_before = {
                row["session_key"]: row["tokens_used"]
                for row in db.execute(
                    """SELECT sample.session_key, sample.tokens_used
                       FROM session_samples AS sample JOIN (
                           SELECT session_key, MAX(observed_at) AS observed_at
                           FROM session_samples WHERE observed_at <= ? GROUP BY session_key
                       ) AS baseline ON baseline.session_key=sample.session_key
                       AND baseline.observed_at=sample.observed_at""",
                    (account_rows[0]["sampled_at"] if account_rows else requested_start,),
                )
            }
            baselines_after = {
                row["session_key"]: row["tokens_used"]
                for row in db.execute(
                    """SELECT sample.session_key, sample.tokens_used
                       FROM session_samples AS sample JOIN (
                           SELECT session_key, MIN(observed_at) AS observed_at
                           FROM session_samples WHERE observed_at >= ? GROUP BY session_key
                       ) AS baseline ON baseline.session_key=sample.session_key
                       AND baseline.observed_at=sample.observed_at""",
                    (account_rows[0]["sampled_at"] if account_rows else requested_start,),
                )
            }
            weekly_baselines_before = {
                row["session_key"]: row["tokens_used"]
                for row in db.execute(
                    """SELECT sample.session_key, sample.tokens_used
                       FROM session_samples AS sample JOIN (
                           SELECT session_key, MAX(observed_at) AS observed_at
                           FROM session_samples WHERE observed_at <= ? GROUP BY session_key
                       ) AS baseline ON baseline.session_key=sample.session_key
                       AND baseline.observed_at=sample.observed_at""",
                    (requested_start,),
                )
            }
            weekly_baselines_after = {
                row["session_key"]: (row["tokens_used"], row["observed_at"])
                for row in db.execute(
                    """SELECT sample.session_key, sample.tokens_used, sample.observed_at
                       FROM session_samples AS sample JOIN (
                           SELECT session_key, MIN(observed_at) AS observed_at
                           FROM session_samples WHERE observed_at >= ? GROUP BY session_key
                       ) AS baseline ON baseline.session_key=sample.session_key
                       AND baseline.observed_at=sample.observed_at""",
                    (requested_start,),
                )
            }
            verified_weekly_by_node = {
                row["node_key"]: int(row["tokens"] or 0)
                for row in db.execute(
                    """SELECT node_key, SUM(tokens_used) AS tokens
                       FROM collector_session_usage WHERE bucket_at >= ? AND bucket_at <= ?
                       GROUP BY node_key""",
                    (requested_start, now),
                )
            }

        coverage_start = account_rows[0]["sampled_at"] if account_rows else None
        account_period_tokens = max(0, int(lifetime) - account_rows[0]["lifetime_tokens"]) if account_rows else 0
        session_period_by_key: dict[str, int] = {}
        for session in all_sessions:
            current = max(0, int(session.get("tokens") or 0))
            if coverage_start is None:
                delta = 0
            elif int(session.get("created_at") or 0) >= coverage_start:
                delta = current
            else:
                baseline = baselines_before.get(session["key"], baselines_after.get(session["key"], current))
                delta = max(0, current - baseline)
            session_period_by_key[session["key"]] = delta
        tracked_period_tokens = sum(session_period_by_key.values())
        period_difference = account_period_tokens - tracked_period_tokens

        node_meta = {node["key"]: node for node in nodes}
        node_tokens: dict[str, int] = {node["key"]: 0 for node in nodes}
        node_maximum_tokens: dict[str, int] = {node["key"]: 0 for node in nodes}
        node_total_tokens: dict[str, int] = {node["key"]: 0 for node in nodes}
        node_partial_sessions: dict[str, int] = {node["key"]: 0 for node in nodes}
        node_coverage_starts: dict[str, list[int]] = {node["key"]: [] for node in nodes}
        node_sessions: dict[str, int] = {node["key"]: 0 for node in nodes}
        for session in all_sessions:
            node_key = session["node_key"]
            current = max(0, int(session.get("tokens") or 0))
            created_at = int(session.get("created_at") or 0)
            updated_at = int(session.get("updated_at") or 0)
            baseline_before = weekly_baselines_before.get(session["key"])
            baseline_after = weekly_baselines_after.get(session["key"])
            partial = False
            possible_unobserved = 0
            if created_at >= requested_start:
                weekly_tokens = current
            elif baseline_before is not None:
                weekly_tokens = max(0, current - baseline_before)
            elif baseline_after is not None:
                weekly_tokens = max(0, current - baseline_after[0])
                if updated_at >= requested_start:
                    partial = True
                    possible_unobserved = min(current, max(0, int(baseline_after[0])))
                    node_coverage_starts.setdefault(node_key, []).append(int(baseline_after[1]))
            else:
                weekly_tokens = 0
                if updated_at >= requested_start:
                    partial = True
                    possible_unobserved = current
            node_tokens[node_key] = node_tokens.get(node_key, 0) + weekly_tokens
            node_maximum_tokens[node_key] = (
                node_maximum_tokens.get(node_key, 0) + weekly_tokens + possible_unobserved
            )
            node_total_tokens[node_key] = node_total_tokens.get(node_key, 0) + current
            if partial:
                node_partial_sessions[node_key] = node_partial_sessions.get(node_key, 0) + 1
            node_sessions[node_key] = node_sessions.get(node_key, 0) + 1
        compared_keys = [key for key in node_tokens if key in node_meta]
        for node_key, verified_tokens in verified_weekly_by_node.items():
            if node_key in node_tokens:
                node_tokens[node_key] = max(node_tokens[node_key], verified_tokens)
                node_maximum_tokens[node_key] = max(node_maximum_tokens[node_key], node_tokens[node_key])
        compared_weekly_tokens = sum(node_tokens[key] for key in compared_keys)
        average = compared_weekly_tokens / len(compared_keys) if compared_keys else 0
        comparison = []
        for node_key in compared_keys:
            node = node_meta[node_key]
            tokens = node_tokens[node_key]
            maximum_tokens = node_maximum_tokens.get(node_key, tokens)
            ratio = tokens / average if average > 0 else 0.0
            status = "high" if len(compared_keys) > 1 and ratio >= 1.5 else (
                "low" if len(compared_keys) > 1 and ratio <= 0.5 else "normal"
            )
            comparison.append({
                "node_key": node_key,
                "hostname": node["hostname"],
                "os_user": node["os_user"],
                "display_name": node.get("display_name") or node["os_user"],
                "tokens": tokens,
                "maximum_tokens": maximum_tokens,
                "attributed_total_tokens": node_total_tokens.get(node_key, 0),
                "partial": maximum_tokens > tokens,
                "partial_session_count": node_partial_sessions.get(node_key, 0),
                "coverage_started_at": (
                    utc_iso(min(node_coverage_starts.get(node_key, [])))
                    if node_coverage_starts.get(node_key) else utc_iso(requested_start)
                ),
                "verified_event_tokens": verified_weekly_by_node.get(node_key, 0),
                "share_percent": tokens / compared_weekly_tokens * 100 if compared_weekly_tokens else 0.0,
                "average_ratio": ratio,
                "status": status,
                "session_count": node_sessions.get(node_key, 0),
                "seen_at": utc_iso(node["seen_at"]),
            })
        comparison.sort(key=lambda item: item["tokens"], reverse=True)

        stale_nodes = [
            node for node in nodes
            if now - int(node["seen_at"]) > max(90, self.config.interval * 6)
        ]
        reasons = []
        if lifetime_difference > 0:
            reasons.append("수집기 설치 전 또는 세션 DB에 남지 않은 계정 사용량이 있습니다.")
        if coverage_start is None or coverage_start > requested_start + 60:
            reasons.append("현재 주기 시작부터 서버 관측 시작 전까지는 증감 대조가 불가능합니다.")
        if period_difference > 0:
            reasons.append("관측 구간에 수집기 미설치 장비, 중지 구간 또는 세션에 귀속되지 않은 사용량이 있습니다.")
        if period_difference < 0:
            reasons.append("세션 집계가 계정 증가량보다 큽니다. 수집 시각 차이 또는 중복 귀속을 확인해야 합니다.")
        if stale_nodes:
            reasons.append(f"최근 보고가 지연된 수집 노드가 {len(stale_nodes)}개 있습니다.")
        return {
            "lifetime": {
                "account_tokens": int(lifetime),
                "attributed_session_tokens": attributed_total,
                "difference_tokens": lifetime_difference,
                "coverage_percent": min(100.0, attributed_total / int(lifetime) * 100) if lifetime else None,
            },
            "period": {
                "requested_started_at": utc_iso(requested_start),
                "coverage_started_at": utc_iso(coverage_start) if coverage_start is not None else None,
                "account_tokens": account_period_tokens,
                "attributed_session_tokens": tracked_period_tokens,
                "difference_tokens": period_difference,
                "coverage_percent": (
                    min(100.0, tracked_period_tokens / account_period_tokens * 100)
                    if account_period_tokens > 0 else None
                ),
            },
            "node_comparison": comparison,
            "reasons": reasons,
        }

    def read_verified_session_usage(
        self, started_at: int, ended_at: int,
    ) -> dict[tuple[str, str], int]:
        with self._lock, self._connect_monitor() as db:
            rows = db.execute(
                """SELECT node_key, session_key, SUM(tokens_used) AS tokens
                   FROM collector_session_usage
                   WHERE bucket_at >= ? AND bucket_at <= ?
                   GROUP BY node_key, session_key""",
                (started_at, ended_at),
            ).fetchall()
        return {
            (row["node_key"], row["session_key"]): int(row["tokens"] or 0)
            for row in rows
        }

    def read_remote(
        self, include_local: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        local_key = self.local_node_key()
        with self._lock, self._connect_monitor() as db:
            if include_local:
                nodes = db.execute(
                    "SELECT node_key, hostname, os_user, account_json, seen_at FROM collector_nodes"
                ).fetchall()
                sessions = db.execute(
                    """SELECT s.*, n.hostname, n.os_user, n.seen_at
                       FROM collector_sessions s JOIN collector_nodes n ON n.node_key=s.node_key"""
                ).fetchall()
            else:
                nodes = db.execute(
                    """SELECT node_key, hostname, os_user, account_json, seen_at
                       FROM collector_nodes WHERE node_key != ?""",
                    (local_key,),
                ).fetchall()
                sessions = db.execute(
                    """SELECT s.*, n.hostname, n.os_user, n.seen_at
                       FROM collector_sessions s JOIN collector_nodes n ON n.node_key=s.node_key
                       WHERE s.node_key != ?""",
                    (local_key,),
                ).fetchall()
        account_by_node = {row["node_key"]: json.loads(row["account_json"]) for row in nodes}
        target_account_key = self.collector_account_key()
        if target_account_key != "unknown":
            account_by_node = {
                node_key: account
                for node_key, account in account_by_node.items()
                if account.get("key") == target_account_key
            }
            nodes = [row for row in nodes if row["node_key"] in account_by_node]
            sessions = [row for row in sessions if row["node_key"] in account_by_node]
        clean_nodes = [
            {"key": row["node_key"], "hostname": row["hostname"], "os_user": row["os_user"],
             "seen_at": row["seen_at"], "account": account_by_node[row["node_key"]]}
            for row in nodes
        ]
        clean_sessions = [
            {"key": row["session_key"], "project_key": row["project_key"], "project_name": row["project_name"],
             "model": row["model"], "tokens": row["tokens_used"], "created_at": row["created_at"],
             "updated_at": row["updated_at"], "archived": bool(row["archived"]), "node_key": row["node_key"],
             "hostname": row["hostname"], "os_user": row["os_user"], "source": "collector"}
            for row in sessions
        ]
        return clean_nodes, clean_sessions

    def read_aliases(self) -> dict[str, str]:
        with self._lock, self._connect_monitor() as db:
            return {row["node_key"]: row["display_name"] for row in db.execute(
                "SELECT node_key, display_name FROM node_aliases"
            )}

    def set_node_alias(self, node_key: str, display_name: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", node_key):
            raise ValueError("invalid node key")
        display_name = " ".join(display_name.strip().split())
        if len(display_name) > 60:
            raise ValueError("display name must be 60 characters or fewer")
        known = node_key == self.local_node_key()
        if not known:
            with self._lock, self._connect_monitor() as db:
                known = db.execute("SELECT 1 FROM collector_nodes WHERE node_key = ?", (node_key,)).fetchone() is not None
        if not known:
            raise ValueError("unknown node")
        with self._lock, self._connect_monitor() as db:
            if display_name:
                db.execute(
                    """INSERT INTO node_aliases (node_key, display_name, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(node_key) DO UPDATE SET display_name=excluded.display_name, updated_at=excluded.updated_at""",
                    (node_key, display_name, int(time.time())),
                )
            else:
                db.execute("DELETE FROM node_aliases WHERE node_key = ?", (node_key,))

    def collect(self, now: int | None = None) -> None:
        now = int(now or time.time())
        _, remote_sessions = self.read_remote()
        sessions = self.read_sessions() + remote_sessions
        visible = [session for session in sessions if not session["archived"]]
        active = sum(now - session["updated_at"] <= self.config.active_window for session in visible)
        session_tokens = sum(session["tokens"] for session in visible)
        account_usage = self.read_account_usage()
        lifetime_tokens = (account_usage or {}).get("summary", {}).get("lifetime_tokens")
        total_tokens = lifetime_tokens if lifetime_tokens is not None else session_tokens
        with self._lock, self._connect_monitor() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO overview_samples
                    (observed_at, total_tokens, session_count, active_count)
                VALUES (?, ?, ?, ?)
                """,
                (now, total_tokens, len(visible), active),
            )
            db.executemany(
                """
                INSERT OR REPLACE INTO session_samples
                    (observed_at, session_key, project_key, model, tokens_used, updated_at, archived)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        now,
                        session["key"],
                        session["project_key"],
                        session["model"],
                        session["tokens"],
                        session["updated_at"],
                        int(session["archived"]),
                    )
                    for session in sessions
                ],
            )
            cutoff = now - 7 * 24 * 60 * 60
            db.execute("DELETE FROM overview_samples WHERE observed_at < ?", (cutoff,))
            db.execute("DELETE FROM session_samples WHERE observed_at < ?", (cutoff,))

    def dashboard(self, now: int | None = None) -> dict[str, Any]:
        now = int(now or time.time())
        collector_nodes, collector_sessions = self.read_remote(include_local=True)
        has_local_collector = any(node["key"] == self.local_node_key() for node in collector_nodes)
        if has_local_collector:
            all_sessions = collector_sessions
        else:
            all_sessions = self.read_sessions() + collector_sessions
        sessions = [session for session in all_sessions if not session["archived"]]
        history_cutoff = now - 24 * 60 * 60
        with self._lock, self._connect_monitor() as db:
            history = db.execute(
                """
                SELECT observed_at, total_tokens, session_count, active_count
                FROM overview_samples
                WHERE observed_at >= ?
                ORDER BY observed_at
                """,
                (history_cutoff,),
            ).fetchall()
            previous = {
                row["session_key"]: row["tokens_used"]
                for row in db.execute(
                    """
                    SELECT sample.session_key, sample.tokens_used
                    FROM session_samples AS sample
                    JOIN (
                        SELECT session_key, MAX(observed_at) AS observed_at
                        FROM session_samples
                        WHERE observed_at < ?
                        GROUP BY session_key
                    ) AS prior
                    ON prior.session_key = sample.session_key
                    AND prior.observed_at = sample.observed_at
                    """,
                    (now - self.config.interval,),
                )
            }

        for session in sessions:
            session["active"] = now - session["updated_at"] <= self.config.active_window
            session["delta"] = max(0, session["tokens"] - previous.get(session["key"], session["tokens"]))

        session_total = sum(session["tokens"] for session in sessions)
        account_usage = self.read_account_usage()
        lifetime_tokens = (account_usage or {}).get("summary", {}).get("lifetime_tokens")
        if has_local_collector:
            nodes = collector_nodes
        else:
            local_node = {
                "key": self.local_node_key(),
                "hostname": socket.gethostname(),
                "os_user": getpass.getuser(),
                "seen_at": now,
                "account": self.read_account(),
            }
            nodes = [local_node] + collector_nodes
        aliases = self.read_aliases()
        for node in nodes:
            node["display_name"] = aliases.get(node["key"], node["os_user"])
        names = {node["key"]: node["display_name"] for node in nodes}
        for session in sessions:
            session["display_user"] = names.get(session["node_key"], session["os_user"])
        weekly_usage = self.read_weekly_usage_history(account_usage, now)
        verified_session_usage: dict[tuple[str, str], int] = {}
        if weekly_usage:
            weekly_started_at = int(datetime.fromisoformat(
                weekly_usage["window_started_at"].replace("Z", "+00:00")
            ).timestamp())
            verified_session_usage = self.read_verified_session_usage(weekly_started_at, now)
        for session in sessions:
            session["verified_weekly_tokens"] = verified_session_usage.get(
                (session["node_key"], session["key"])
            )
        reconciliation = self.read_usage_reconciliation(
            all_sessions, nodes, account_usage, weekly_usage, now,
        )
        weekly_account_tokens = (
            reconciliation["period"]["account_tokens"] if reconciliation is not None else None
        )
        for session in sessions:
            session["created_at"] = utc_iso(session["created_at"])
            session["updated_at"] = utc_iso(session["updated_at"])
        if account_usage:
            reporter = next(
                (node for node in nodes if node["key"] == account_usage["reported_by_node_key"]), None
            )
            account_usage["observed_at"] = utc_iso(account_usage["observed_at"])
            account_usage["received_at"] = utc_iso(account_usage["received_at"])
            account_usage["reported_by"] = {
                "node_key": account_usage.pop("reported_by_node_key"),
                "hostname": reporter["hostname"] if reporter else None,
                "os_user": reporter["os_user"] if reporter else None,
                "display_name": reporter["display_name"] if reporter else None,
            }
            for limit in account_usage.get("rate_limits", []):
                for window_name in ("primary", "secondary"):
                    window = limit.get(window_name)
                    if window and window.get("resets_at") is not None:
                        window["resets_at_iso"] = utc_iso(window["resets_at"])
        return {
            "summary": {
                "total_tokens": lifetime_tokens if lifetime_tokens is not None else session_total,
                "lifetime_tokens": lifetime_tokens,
                "weekly_account_tokens": weekly_account_tokens,
                "session_tokens": session_total,
                "account_usage_available": lifetime_tokens is not None,
                "session_count": len(sessions),
                "active_count": sum(session["active"] for session in sessions),
                "project_count": len({session["project_key"] for session in sessions}),
                "model_count": len({session["model"] for session in sessions}),
            },
            "sessions": sessions,
            "nodes": nodes,
            "account_usage": account_usage,
            "weekly_usage": weekly_usage,
            "reconciliation": reconciliation,
            "history": [
                {
                    "at": utc_iso(row["observed_at"]),
                    "tokens": row["total_tokens"],
                    "sessions": row["session_count"],
                    "active": row["active_count"],
                }
                for row in history
            ],
            "meta": {
                "collected_at": utc_iso(now),
                "refresh_seconds": self.config.interval,
                "active_window_seconds": self.config.active_window,
                "privacy": "No titles, prompts, responses, paths, or raw session IDs are collected.",
            },
        }


class Collector(threading.Thread):
    def __init__(self, store: UsageStore):
        super().__init__(daemon=True, name="usage-collector")
        self.store = store
        self.stopped = threading.Event()
        self.last_error: str | None = None
        self.account_usage_error: str | None = None
        self.next_account_usage_at = 0.0

    def run(self) -> None:
        while not self.stopped.is_set():
            try:
                self.store.collect()
                self.last_error = None
            except (OSError, sqlite3.Error) as error:
                self.last_error = str(error)
            if time.monotonic() >= self.next_account_usage_at:
                self.next_account_usage_at = time.monotonic() + 30
                try:
                    live = read_app_server_usage(self.store.config.auth_file.parent)
                    local_email = str(self.store.read_account().get("email") or "").casefold()
                    live_email = str(live.get("account", {}).get("email") or "").casefold()
                    if local_email and live_email and local_email != live_email:
                        raise RuntimeError("Codex app-server account does not match the registered account")
                    self.store.store_local_account_usage(live["usage"])
                    self.account_usage_error = None
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    self.account_usage_error = f"{type(error).__name__}: account usage unavailable"
            self.stopped.wait(self.store.config.interval)


def build_handler(store: UsageStore, collector: Collector):
    class DashboardHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[{self.log_date_time_string()}] {format % args}")

        def send_json(
            self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK,
            response_token: str | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if response_token is not None:
                signature = hmac.new(response_token.encode(), body, hashlib.sha256).hexdigest()
                self.send_header("X-Codex-Response-Signature", signature)
            self.end_headers()
            self.wfile.write(body)

        def dashboard_authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(header[6:]).decode()
                username, password = decoded.split(":", 1)
            except (ValueError, UnicodeDecodeError):
                return False
            return hmac.compare_digest(username, store.dashboard_user) and hmac.compare_digest(
                password, store.dashboard_password
            )

        def request_dashboard_login(self) -> None:
            body = b"Authentication required"
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="Codex Usage Monitor", charset="UTF-8"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/health":
                self.send_json({
                    "ok": collector.last_error is None,
                    "error": collector.last_error,
                    "account_usage_error": collector.account_usage_error,
                })
                return
            if not self.dashboard_authorized():
                self.request_dashboard_login()
                return
            if path == "/api/dashboard":
                try:
                    self.send_json(store.dashboard())
                except (OSError, sqlite3.Error) as error:
                    self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if path == "/":
                self.path = "/index.html"
            super().do_GET()

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/ingest":
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 1_048_576:
                    raise ValueError("invalid payload size")
                body = self.rfile.read(size)
                timestamp = self.headers.get("X-Codex-Timestamp", "")
                signature = self.headers.get("X-Codex-Signature", "")
                if abs(int(time.time()) - int(timestamp)) > 300:
                    raise PermissionError("expired request")
                expected = hmac.new(
                    store.collector_token.encode(), timestamp.encode() + b"." + body, hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    raise PermissionError("invalid signature")
                payload = json.loads(body)
                store.ingest(payload)
                store.collect()
                node_key = str((payload.get("node") or {}).get("key") or "")
                response = store.collector_response(node_key, payload.get("collector"))
                self.send_json(
                    response, HTTPStatus.ACCEPTED, response_token=store.collector_token,
                )
            except PermissionError as error:
                self.send_json({"error": str(error)}, HTTPStatus.UNAUTHORIZED)
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            match = re.fullmatch(r"/api/nodes/([A-Za-z0-9_-]{1,64})/alias", path)
            if not match:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            if not self.dashboard_authorized():
                self.request_dashboard_login()
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 4096:
                    raise ValueError("invalid payload size")
                payload = json.loads(self.rfile.read(size))
                display_name = payload.get("display_name")
                if not isinstance(display_name, str):
                    raise ValueError("display_name must be a string")
                store.set_node_alias(match.group(1), display_name)
                self.send_json({"ok": True, "display_name": display_name.strip()})
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    return DashboardHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("CODEX_MONITOR_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CODEX_MONITOR_PORT", "8765")))
    parser.add_argument(
        "--source-db",
        type=Path,
        default=Path(os.getenv("CODEX_STATE_DB", DEFAULT_SOURCE_DB)),
        help="Path to Codex state_5.sqlite",
    )
    parser.add_argument(
        "--monitor-db",
        type=Path,
        default=Path(os.getenv("CODEX_MONITOR_DB", DEFAULT_MONITOR_DB)),
    )
    parser.add_argument("--interval", type=int, default=int(os.getenv("CODEX_MONITOR_INTERVAL", "10")))
    parser.add_argument("--active-window", type=int, default=300)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.interval < 2:
        raise SystemExit("--interval must be at least 2 seconds")
    config = Config(
        source_db=args.source_db.expanduser().resolve(),
        monitor_db=args.monitor_db.expanduser().resolve(),
        salt=os.getenv("CODEX_MONITOR_HASH_SALT", "codex-usage-monitor-local"),
        interval=args.interval,
        active_window=args.active_window,
    )
    store = UsageStore(config)
    store.collect()
    collector = Collector(store)
    collector.start()
    server = ThreadingHTTPServer((args.host, args.port), build_handler(store, collector))
    print(f"Codex Usage Monitor: http://{args.host}:{args.port}")
    print(f"Reading: {config.source_db}")
    print(f"Collector token: {config.collector_token_file}")
    print(f"Dashboard login: {store.dashboard_user} / password in {config.dashboard_password_file}")
    print("Privacy: titles, prompts, responses, paths, and raw IDs are excluded")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stopped.set()
        server.server_close()


if __name__ == "__main__":
    main()
