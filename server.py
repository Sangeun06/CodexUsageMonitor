#!/usr/bin/env python3
"""Privacy-first local Codex token usage dashboard."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from contextvars import ContextVar
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
from datetime import datetime, time as datetime_time, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

from account_registry import initialize_accounts
from quota_usage import read_quota_usage

from collector import COLLECTOR_VERSION, read_app_server_usage
from usage_analytics import EVENT_RETENTION_DAYS, read_analytics
from usage_aggregation import (
    UsageRepository, UsageWindow, aggregate_users, attribute_sessions,
    build_weekly_breakdown, utc_iso,
)


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DEFAULT_SOURCE_DB = Path.home() / ".codex" / "state_5.sqlite"
DEFAULT_MONITOR_DB = APP_DIR / "data" / "monitor.sqlite"
DEFAULT_AUTH_FILE = Path.home() / ".codex" / "auth.json"
DEFAULT_COLLECTOR_TOKEN = APP_DIR / "data" / "collector.token"
DEFAULT_DASHBOARD_PASSWORD = APP_DIR / "data" / "dashboard.password"


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
        self._account_context = ContextVar("monitor_account", default=None)
        config.monitor_db.parent.mkdir(parents=True, exist_ok=True)
        self.collector_token = self._load_or_create_collector_token()
        self.dashboard_user = os.getenv("CODEX_DASHBOARD_USER", "admin")
        self.dashboard_password = self._load_or_create_dashboard_password()
        self._initialize_monitor_db()
        with self._connect_monitor() as db:
            initialize_accounts(db, self.read_account(self.collector_token))

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
                    node_key TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    model TEXT NOT NULL,
                    tokens_used INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    archived INTEGER NOT NULL,
                    PRIMARY KEY (observed_at, node_key, session_key)
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
                CREATE INDEX IF NOT EXISTS idx_collector_session_usage_bucket_at
                    ON collector_session_usage(bucket_at);
                CREATE TABLE IF NOT EXISTS collector_usage_details (
                    account_key TEXT NOT NULL, node_key TEXT NOT NULL, session_key TEXT NOT NULL,
                    bucket_at INTEGER NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL,
                    service_tier TEXT NOT NULL, reasoning_mode TEXT NOT NULL,
                    tokens INTEGER NOT NULL, events INTEGER NOT NULL,
                    input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
                    reasoning_output_tokens INTEGER,
                    PRIMARY KEY (account_key, node_key, session_key, bucket_at, model, effort, service_tier, reasoning_mode)
                );
                CREATE INDEX IF NOT EXISTS idx_usage_details_account_time ON collector_usage_details(account_key, bucket_at);
                CREATE TABLE IF NOT EXISTS collector_usage_metadata (
                    account_key TEXT NOT NULL, node_key TEXT NOT NULL, version TEXT NOT NULL,
                    backfill_pending INTEGER NOT NULL, received_at INTEGER NOT NULL,
                    PRIMARY KEY(account_key, node_key)
                );
                """
            )
            db.execute("BEGIN IMMEDIATE")
            metadata_columns = {row["name"] for row in db.execute("PRAGMA table_info(collector_usage_metadata)")}
            if "kind" not in metadata_columns:
                db.execute("ALTER TABLE collector_usage_metadata ADD COLUMN kind TEXT NOT NULL DEFAULT 'unknown'")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(session_samples)")}
            if "node_key" not in columns:
                # Old snapshots used only the session hash. Preserve every row,
                # assigning it to a node only when its origin is unambiguous.
                db.execute("""CREATE TABLE session_samples_migrated (
                    observed_at INTEGER NOT NULL, node_key TEXT NOT NULL,
                    session_key TEXT NOT NULL, project_key TEXT NOT NULL,
                    model TEXT NOT NULL, tokens_used INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL, archived INTEGER NOT NULL,
                    PRIMARY KEY (observed_at, node_key, session_key)
                )""")
                db.execute(
                    """INSERT INTO session_samples_migrated
                       SELECT sample.observed_at,
                              CASE WHEN origins.node_count = 1 THEN origins.node_key
                                   WHEN origins.node_count > 1 THEN '' ELSE ? END,
                              sample.session_key, sample.project_key, sample.model,
                              sample.tokens_used, sample.updated_at, sample.archived
                       FROM session_samples AS sample LEFT JOIN (
                           SELECT session_key, COUNT(DISTINCT node_key) AS node_count,
                                  MIN(node_key) AS node_key
                           FROM collector_sessions GROUP BY session_key
                       ) AS origins ON origins.session_key = sample.session_key""",
                    (self.local_node_key(),),
                )
                db.execute("DROP TABLE session_samples")
                db.execute("ALTER TABLE session_samples_migrated RENAME TO session_samples")
            db.execute("""CREATE INDEX IF NOT EXISTS idx_session_samples_node_key_time
                          ON session_samples(node_key, session_key, observed_at DESC)""")

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
        nodes, all_sessions = self.read_current_sessions(now)
        aliases = self.read_aliases()
        for node in nodes:
            node["display_name"] = aliases.get(node["key"], node["os_user"])
        attribution = self.read_period_attribution(all_sessions, nodes, UsageWindow.weekly(weekly, now))
        with self._lock, self._connect_monitor() as db:
            quota = read_quota_usage(db, self.collector_account_key(), weekly, aggregate_users(attribution), now)
        comparison = [item for item in quota["user_usage"] if (item["used_percent"] or 0) > 0]
        if not comparison:
            return []
        target = max(comparison, key=lambda item: item["used_percent"])
        resets_at = str(weekly.get("resets_at") or "unknown")
        band = max(0, min(100, int(used // 10) * 10))
        alert_key = hashlib.sha256(
            f"weekly-quota-pace-150:{self.collector_account_key()}:{resets_at}:{band}:{target['node_key']}".encode()
        ).hexdigest()[:24]
        share = target["used_percent"]
        payload = {
            "id": alert_key,
            "kind": "weekly-pace-150",
            "account_key": self.collector_account_key(),
            "severity": "warning",
            "title": "Codex 주간 사용 속도 경고",
            "message": (
                f"공유 계정 사용률 {used:.1f}%가 현재 권장선 {recommended:.1f}%의 "
                f"150%를 넘었습니다. 이 장비의 관측 기반 추정 소모량은 주간 제공 한도의 {share:.1f}%입니다."
            ),
            "used_percent": used,
            "node_used_percent": share,
            "attribution_basis": "rate_limit_delta",
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
                   WHERE node_key = ? AND delivered_at IS NULL
                   AND COALESCE(json_extract(payload_json, '$.account_key'), ?) = ?
                   AND json_extract(payload_json, '$.attribution_basis') = 'rate_limit_delta'
                   AND json_extract(payload_json, '$.resets_at') = ?
                   ORDER BY created_at LIMIT 8""",
                (node_key, self.collector_account_key(), self.collector_account_key(), resets_at),
            ).fetchall()
        return [item for row in rows if (item := json.loads(row["payload_json"])).get("account_key", self.collector_account_key()) == self.collector_account_key()]

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
        selected = self._account_context.get()
        if selected is not None:
            return selected
        with self._connect_monitor() as db:
            row = db.execute("SELECT value FROM monitor_settings WHERE key='default_account'").fetchone()
        return row[0] if row else "unknown"

    @contextmanager
    def account_scope(self, account_key):
        token = self._account_context.set(account_key)
        try:
            yield
        finally:
            self._account_context.reset(token)

    def accounts(self):
        with self._lock, self._connect_monitor() as db:
            registered = [dict(row) for row in db.execute("SELECT * FROM monitored_accounts ORDER BY created_at, account_key")]
            candidates = [dict(row) for row in db.execute("SELECT * FROM discovered_accounts ORDER BY seen_at DESC")]
            default = db.execute("SELECT value FROM monitor_settings WHERE key='default_account'").fetchone()
        for row in registered + candidates:
            row["account"] = json.loads(row.pop("account_json"))
            if "enabled" in row:
                row["enabled"] = bool(row["enabled"])
        local = self.read_account(self.collector_token)
        if local["key"] != "unknown" and not any(row["account_key"] == local["key"] for row in candidates):
            candidates.insert(0, {"account_key": local["key"], "account": local, "seen_at": None})
        return {"registered": registered, "discovered": candidates, "default_account_key": default[0] if default else None}

    def save_account(self, account_key, label=None, enabled=True, make_default=False):
        if not isinstance(account_key, str) or not re.fullmatch(r"[a-f0-9]{12}", account_key):
            raise ValueError("account key must be 12 hexadecimal characters")
        if not isinstance(enabled, bool) or not isinstance(make_default, bool):
            raise ValueError("enabled and make_default must be booleans")
        if label is not None and (not isinstance(label, str) or not label.strip() or len(label.strip()) > 120):
            raise ValueError("label must contain 1 to 120 characters")
        account = next((row["account"] for row in self.accounts()["discovered"] if row["account_key"] == account_key), {"key": account_key})
        with self._lock, self._connect_monitor() as db:
            existing = db.execute("SELECT * FROM monitored_accounts WHERE account_key=?", (account_key,)).fetchone()
            display = label.strip() if label is not None else (existing["label"] if existing else account.get("email") or account.get("name") or account_key)
            db.execute("""INSERT INTO monitored_accounts VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_key) DO UPDATE SET label=excluded.label, enabled=excluded.enabled""",
                (account_key, display, json.dumps(account), int(enabled), int(time.time())))
            if make_default or not db.execute("SELECT 1 FROM monitor_settings WHERE key='default_account'").fetchone():
                db.execute("INSERT OR REPLACE INTO monitor_settings VALUES ('default_account', ?)", (account_key,))

    def discover_account(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("account"), dict):
            raise ValueError("invalid account discovery")
        account = payload["account"]
        key = account.get("key")
        if key != "unknown":
            if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{12}", key):
                raise ValueError("invalid account key")
            clean = {field: str(account[field])[:120] for field in ("key", "name", "email", "plan")
                     if isinstance(account.get(field), str)}
            with self._lock, self._connect_monitor() as db:
                db.execute("""INSERT INTO discovered_accounts VALUES (?, ?, ?)
                    ON CONFLICT(account_key) DO UPDATE SET account_json=excluded.account_json, seen_at=excluded.seen_at""",
                    (key, json.dumps(clean), int(time.time())))
        registered = self.accounts()["registered"]
        return {"tracked_accounts": [row["account_key"] for row in registered],
                "monitored_accounts": [row["account_key"] for row in registered if row["enabled"]]}

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
        if not isinstance(payload, dict):
            raise ValueError("invalid collector payload")
        node = payload.get("node", {})
        account = payload.get("account", {})
        sessions = payload.get("sessions", [])
        if not isinstance(node, dict) or not isinstance(account, dict):
            raise ValueError("invalid collector node or account")
        required = ("key", "hostname", "os_user")
        if not all(isinstance(node.get(field), str) and node[field] for field in required):
            raise ValueError("invalid collector node")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", node["key"]):
            raise ValueError("invalid collector node key")
        if not isinstance(sessions, list) or len(sessions) > 10_000:
            raise ValueError("invalid sessions")
        clean_account = {
            field: account.get(field)
            for field in ("key", "name", "email", "email_verified", "plan", "auth_provider", "auth_mode")
            if account.get(field) is not None
        }
        account_key = clean_account.get("key")
        with self._lock, self._connect_monitor() as db:
            allowed = db.execute("SELECT 1 FROM monitored_accounts WHERE account_key=? AND enabled=1", (account_key,)).fetchone()
        if not allowed:
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
        clean_details = []
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
            raw_details = session.get("usage_details", [])
            if not isinstance(raw_details, list):
                raise ValueError("invalid usage details")
            for detail in raw_details:
                if not isinstance(detail, dict):
                    raise ValueError("invalid usage detail")
                at, tokens, events = (self._nonnegative_int(detail.get(field)) for field in ("at", "tokens", "events"))
                if at is None or at % 60 or tokens is None or events is None or events < 1:
                    raise ValueError("invalid usage detail counters")
                metadata = [detail.get(field, "unknown") for field in ("model", "effort", "service_tier", "reasoning_mode")]
                if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value) for value in metadata):
                    raise ValueError("invalid usage detail metadata")
                counts = [self._nonnegative_int(detail.get(field)) for field in (
                    "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
                )]
                if counts[0] is not None and counts[1] is not None and counts[1] > counts[0]:
                    raise ValueError("cached tokens exceed input tokens")
                clean_details.append((account_key, node["key"][:64], session_key, at, *metadata, tokens, events, *counts))
                if len(clean_details) > 50_000:
                    raise ValueError("too many usage details")
        with self._lock, self._connect_monitor() as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(
                """INSERT INTO collector_nodes (node_key, hostname, os_user, account_json, seen_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(node_key) DO UPDATE SET hostname=excluded.hostname,
                   os_user=excluded.os_user, account_json=excluded.account_json, seen_at=excluded.seen_at""",
                (node["key"][:64], node["hostname"][:120], node["os_user"][:120], json.dumps(clean_account), now),
            )
            db.execute("""INSERT INTO collector_node_accounts VALUES (?, ?, ?, ?)
                ON CONFLICT(account_key, node_key) DO UPDATE SET
                account_json=json_patch(collector_node_accounts.account_json, excluded.account_json), seen_at=excluded.seen_at""",
                (account_key, node["key"], json.dumps(clean_account), now))
            db.execute("DELETE FROM collector_sessions WHERE node_key = ? AND account_key=?", (node["key"], account_key))
            db.executemany(
                """INSERT INTO collector_sessions
                   (node_key, session_key, project_key, project_name, model, tokens_used, created_at, updated_at, archived, account_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(*session, account_key) for session in clean_sessions],
            )
            db.executemany(
                """INSERT INTO collector_session_usage
                   (node_key, session_key, bucket_at, tokens_used, account_key) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(account_key, node_key, session_key, bucket_at) DO UPDATE SET
                   tokens_used=MAX(tokens_used, excluded.tokens_used)""",
                [(*bucket, account_key) for bucket in clean_usage_buckets],
            )
            db.executemany(
                """INSERT INTO collector_usage_details VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account_key,node_key,session_key,bucket_at,model,effort,service_tier,reasoning_mode)
                   DO UPDATE SET tokens=excluded.tokens, events=excluded.events,
                   input_tokens=COALESCE(excluded.input_tokens,input_tokens),
                   cached_input_tokens=COALESCE(excluded.cached_input_tokens,cached_input_tokens),
                   output_tokens=COALESCE(excluded.output_tokens,output_tokens),
                   reasoning_output_tokens=COALESCE(excluded.reasoning_output_tokens,reasoning_output_tokens)
                   WHERE excluded.tokens>=collector_usage_details.tokens AND excluded.events>=collector_usage_details.events""",
                clean_details,
            )
            if clean_details:
                # Details and legacy minute counters describe overlapping work.
                # Recover missing history with MAX, never by adding both.
                db.execute(
                    """INSERT INTO collector_session_usage (node_key,session_key,bucket_at,tokens_used,account_key)
                       SELECT node_key,session_key,bucket_at,SUM(tokens),account_key FROM collector_usage_details
                       WHERE account_key=? AND node_key=? AND bucket_at>=?
                       GROUP BY account_key,node_key,session_key,bucket_at
                       ON CONFLICT(account_key,node_key,session_key,bucket_at) DO UPDATE SET
                       tokens_used=MAX(tokens_used,excluded.tokens_used)""",
                    (account_key, node["key"], now - 14 * 86400),
                )
            collector_info = payload.get("collector") or {}
            if isinstance(collector_info, dict):
                pending = sum(session.get("usage_detail_backfill_complete") is False for session in sessions)
                db.execute("INSERT OR REPLACE INTO collector_usage_metadata (account_key,node_key,version,backfill_pending,received_at,kind) VALUES (?,?,?,?,?,?)",
                           (account_key, node["key"], str(collector_info.get("version") or "unknown")[:40], pending, now,
                            str(collector_info.get("kind") or "unknown")[:40]))
            db.execute("DELETE FROM collector_usage_details WHERE bucket_at < ?", (now - EVENT_RETENTION_DAYS * 86400,))
            db.execute(
                "DELETE FROM collector_session_usage WHERE bucket_at < ?",
                (now - EVENT_RETENTION_DAYS * 86400,),
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
        reported_limits = usage["rate_limits"]
        lifetime = usage["summary"]["lifetime_tokens"]
        current = db.execute(
            "SELECT * FROM account_usage WHERE account_key = ?", (account_key,)
        ).fetchone()
        # Validate freshness before writing either the snapshot or its history.
        if current is not None:
            if usage["observed_at"] < current["observed_at"]:
                return
            if lifetime is not None and current["lifetime_tokens"] is not None and lifetime < current["lifetime_tokens"]:
                return
            previous = json.loads(current["usage_json"])
            usage["summary"] = {
                field: value if value is not None else previous.get("summary", {}).get(field)
                for field, value in usage["summary"].items()
            }
            if lifetime is None:
                usage["daily_usage"] = previous.get("daily_usage", [])
            if not usage["rate_limits"]:
                usage["rate_limits"] = previous.get("rate_limits", [])
        sample_at = received_at - received_at % 30
        for limit in reported_limits:
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
        if lifetime is not None:
            db.execute(
                """INSERT INTO account_token_samples
                   (account_key, sampled_at, lifetime_tokens) VALUES (?, ?, ?)
                   ON CONFLICT(account_key, sampled_at) DO UPDATE SET
                   lifetime_tokens=MIN(lifetime_tokens, excluded.lifetime_tokens)""",
                (account_key, received_at, lifetime),
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
             usage["observed_at"], received_at, usage["summary"]["lifetime_tokens"]),
        )

    def store_local_account_usage(self, usage: dict[str, Any], now: int | None = None) -> None:
        now = int(now or time.time())
        account_key = self.read_account(self.collector_token)["key"]
        if not any(row["account_key"] == account_key and row["enabled"] for row in self.accounts()["registered"]):
            raise RuntimeError("local Codex account is not registered")
        with self._lock, self._connect_monitor() as db:
            self._store_account_usage(db, account_key, self.local_node_key(), usage, now)

    def read_account_usage(self) -> dict[str, Any] | None:
        account_key = self.collector_account_key()
        with self._lock, self._connect_monitor() as db:
            row = db.execute("SELECT * FROM account_usage WHERE account_key = ?", (account_key,)).fetchone()
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
                   AND resets_at = ? AND sampled_at >= ? AND sampled_at <= ? ORDER BY sampled_at""",
                (account_usage["account_key"], limit_id, window_kind, resets_at, window_start, now),
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

    def read_period_attribution(self, sessions, nodes, window):
        with self._lock, self._connect_monitor() as db:
            repository = UsageRepository(db, self.collector_account_key())
            events = repository.verified_sessions(window)
            keys = {(session["node_key"], session["key"]) for session in sessions} | set(events)
            baselines = repository.first_snapshots(window, keys)
        return attribute_sessions(sessions, {node["key"] for node in nodes}, window, events, baselines)

    def read_usage_reconciliation(
        self, all_sessions, nodes, account_usage, weekly_usage, now,
        weekly_attribution=None,
    ) -> dict[str, Any] | None:
        if not account_usage or account_usage.get("summary", {}).get("lifetime_tokens") is None:
            return None
        lifetime = int(account_usage["summary"]["lifetime_tokens"])
        attributed_total = sum(max(0, int(session.get("tokens") or 0)) for session in all_sessions)
        lifetime_difference = lifetime - attributed_total
        window = UsageWindow.weekly(weekly_usage, now) if weekly_usage else UsageWindow(now - 7 * 86400, now)
        with self._lock, self._connect_monitor() as db:
            account_rows = db.execute(
                """SELECT sampled_at, lifetime_tokens FROM account_token_samples
                   WHERE account_key = ? AND sampled_at >= ? AND sampled_at <= ? ORDER BY sampled_at""",
                (account_usage["account_key"], window.start, window.end),
            ).fetchall()
        coverage_start = account_rows[0]["sampled_at"] if account_rows else None
        account_period_tokens = max(0, lifetime - account_rows[0]["lifetime_tokens"]) if account_rows else 0
        period_attribution = self.read_period_attribution(
            all_sessions, nodes, UsageWindow(coverage_start, window.end),
        ) if coverage_start is not None else {}
        period_users = aggregate_users(period_attribution)
        tracked_period_tokens = sum(item["tokens"] for item in period_users)
        period_difference = account_period_tokens - tracked_period_tokens
        if weekly_attribution is None:
            weekly_attribution = self.read_period_attribution(all_sessions, nodes, window)
        weekly_users = {item["node_key"]: item["tokens"] for item in aggregate_users(weekly_attribution)}
        compared_weekly_tokens = sum(weekly_users.values())
        comparison = []
        for node in nodes:
            key = node["key"]
            amounts = [amount for (node_key, _), amount in weekly_attribution.items() if node_key == key]
            node_sessions = [session for session in all_sessions if session["node_key"] == key]
            tokens = weekly_users.get(key, 0)
            maximum = sum(item.maximum_tokens for item in amounts)
            partial = [item for item in amounts if item.maximum_tokens > item.tokens]
            comparison.append({
                "node_key": key, "hostname": node["hostname"], "os_user": node["os_user"],
                "display_name": node.get("display_name") or node["os_user"],
                "tokens": tokens, "maximum_tokens": maximum,
                "attributed_total_tokens": sum(session["tokens"] for session in node_sessions),
                "partial": maximum > tokens, "partial_session_count": len(partial),
                "coverage_started_at": utc_iso(min((item.coverage_start for item in partial), default=window.start)),
                "verified_event_tokens": sum(item.event_tokens for item in amounts),
                "snapshot_only_tokens": sum(item.snapshot_only_tokens for item in amounts),
                "share_percent": tokens / compared_weekly_tokens * 100 if compared_weekly_tokens else 0.0,
                "basis": "token_throughput", "status": "throughput", "session_count": len(node_sessions),
                "seen_at": utc_iso(node["seen_at"]),
            })
        comparison.sort(key=lambda item: (-item["tokens"], item["node_key"]))
        stale_nodes = [node for node in nodes if now - int(node["seen_at"]) > max(90, self.config.interval * 6)]
        reasons = []
        if lifetime_difference > 0:
            reasons.append("수집기 설치 전 또는 세션 DB에 남지 않은 계정 사용량이 있습니다.")
        if coverage_start is None or coverage_start > window.start + 60:
            reasons.append("현재 주기 시작부터 서버 관측 시작 전까지는 증감 대조가 불가능합니다.")
        if period_difference > 0:
            reasons.append("관측 구간에 수집기 미설치 장비, 중지 구간 또는 세션에 귀속되지 않은 사용량이 있습니다.")
        if period_difference < 0:
            reasons.append("세션 집계가 계정 증가량보다 큽니다. 수집 시각 차이 또는 중복 귀속을 확인해야 합니다.")
        if stale_nodes:
            reasons.append(f"최근 보고가 지연된 수집 노드가 {len(stale_nodes)}개 있습니다.")
        return {
            "lifetime": {
                "account_tokens": lifetime, "attributed_session_tokens": attributed_total,
                "difference_tokens": lifetime_difference,
                "coverage_percent": min(100.0, attributed_total / lifetime * 100) if lifetime else None,
            },
            "period": {
                "requested_started_at": utc_iso(window.start),
                "coverage_started_at": utc_iso(coverage_start) if coverage_start is not None else None,
                "account_sample_count": len(account_rows), "account_tokens": account_period_tokens,
                "attributed_session_tokens": tracked_period_tokens, "difference_tokens": period_difference,
                "untracked_tokens": max(0, period_difference), "over_attributed_tokens": max(0, -period_difference),
                "participant_count": len(period_users), "user_tokens": period_users,
                "coverage_percent": min(100.0, tracked_period_tokens / account_period_tokens * 100) if account_period_tokens > 0 else None,
            },
            "node_comparison": comparison, "reasons": reasons,
        }

    def read_weekly_token_breakdown(
        self, account_usage, weekly, nodes, reconciliation, now, attribution=None,
    ) -> dict[str, Any] | None:
        if not account_usage or not weekly:
            return None
        window = UsageWindow.weekly(weekly, now)
        if attribution is None:
            _, sessions = self.read_current_sessions(now)
            attribution = self.read_period_attribution(sessions, nodes, window)
        return build_weekly_breakdown(account_usage, window, attribution, (reconciliation or {}).get("period") or {})

    def read_verified_session_usage(self, started_at: int, ended_at: int) -> dict[tuple[str, str], int]:
        with self._lock, self._connect_monitor() as db:
            return UsageRepository(db, self.collector_account_key()).verified_sessions(UsageWindow(started_at, ended_at))

    def read_daily_user_trend(
        self, nodes: list[dict[str, Any]], now: int,
    ) -> dict[str, Any]:
        """Aggregate confirmed session events into 14 local calendar days."""
        today = datetime.fromtimestamp(now).date()
        first_day = today - timedelta(days=13)
        start_at = int(datetime.combine(first_day, datetime_time.min).timestamp())
        visible_nodes = {node["key"] for node in nodes}
        days = []
        by_date = {}
        for offset in range(14):
            day = first_day + timedelta(days=offset)
            item = {
                "date": day.isoformat(),
                "day_type": "weekend" if day.weekday() >= 5 else "weekday",
                "total_tokens": 0,
                "users": {},
            }
            days.append(item)
            by_date[item["date"]] = item
        with self._lock, self._connect_monitor() as db:
            rows = UsageRepository(db, self.collector_account_key()).daily_events(UsageWindow(start_at, now))
        for row in rows:
            if row["node_key"] not in visible_nodes:
                continue
            day = by_date.get(row["usage_date"])
            if day is None:
                continue
            tokens = int(row["tokens"])
            day["total_tokens"] += tokens
            day["users"][row["node_key"]] = day["users"].get(row["node_key"], 0) + tokens
        for day in days:
            day["users"] = dict(sorted(day["users"].items()))
        return {"timezone": datetime.fromtimestamp(now).astimezone().tzname(), "days": days}

    def read_remote(
        self, include_local: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        account_key = self.collector_account_key()
        with self._lock, self._connect_monitor() as db:
            nodes = db.execute("""SELECT n.node_key, n.hostname, n.os_user, a.account_json, a.seen_at
                FROM collector_node_accounts a JOIN collector_nodes n USING(node_key)
                WHERE a.account_key=?""", (account_key,)).fetchall()
            sessions = db.execute("""SELECT s.*, n.hostname, n.os_user
                FROM collector_sessions s JOIN collector_nodes n USING(node_key)
                WHERE s.account_key=?""", (account_key,)).fetchall()
        if not include_local:
            nodes = [row for row in nodes if row["node_key"] != self.local_node_key()]
            sessions = [row for row in sessions if row["node_key"] != self.local_node_key()]
        account_by_node = {row["node_key"]: json.loads(row["account_json"]) for row in nodes}
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

    def read_current_sessions(
        self, now: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        nodes, sessions = self.read_remote(include_local=True)
        if any(node["key"] == self.local_node_key() for node in nodes):
            return nodes, sessions
        local_account = self.read_account(self.collector_token)
        if local_account["key"] != self.collector_account_key():
            return nodes, sessions
        # The raw local database has no per-account counters. Once several
        # targets exist, only the collector's attributed ledger is usable.
        if len(self.accounts()["registered"]) > 1:
            return nodes, sessions
        try:
            local_sessions = self.read_sessions()
        except FileNotFoundError:
            local_sessions = []
        local_node = {
            "key": self.local_node_key(), "hostname": socket.gethostname(),
            "os_user": getpass.getuser(), "seen_at": now, "account": local_account,
        }
        return [local_node] + nodes, local_sessions + sessions

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
        for account in self.accounts()["registered"]:
            if account["enabled"]:
                with self.account_scope(account["account_key"]):
                    self._collect_account(now)

    def _collect_account(self, now: int | None = None) -> None:
        now = int(now or time.time())
        _, sessions = self.read_current_sessions(now)
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
                    (observed_at, total_tokens, session_count, active_count, account_key)
                VALUES (?, ?, ?, ?, ?)
                """,
                (now, total_tokens, len(visible), active, self.collector_account_key()),
            )
            db.executemany(
                """
                INSERT OR REPLACE INTO session_samples
                    (observed_at, node_key, session_key, project_key, model, tokens_used, updated_at, archived, account_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        now,
                        session["node_key"],
                        session["key"],
                        session["project_key"],
                        session["model"],
                        session["tokens"],
                        session["updated_at"],
                        int(session["archived"]),
                        self.collector_account_key(),
                    )
                    for session in sessions
                ],
            )
            cutoff = now - 7 * 24 * 60 * 60
            db.execute("DELETE FROM overview_samples WHERE observed_at < ?", (cutoff,))
            db.execute("DELETE FROM session_samples WHERE observed_at < ?", (cutoff,))

    def dashboard(self, now: int | None = None, account_key: str | None = None) -> dict[str, Any]:
        selected = account_key or self.collector_account_key()
        if account_key and not any(row["account_key"] == selected for row in self.accounts()["registered"]):
            raise ValueError("unknown monitoring account")
        with self.account_scope(selected):
            return self._dashboard(now)

    def _dashboard(self, now: int | None = None) -> dict[str, Any]:
        now = int(now or time.time())
        nodes, all_sessions = self.read_current_sessions(now)
        sessions = [session for session in all_sessions if not session["archived"]]
        history_cutoff = now - 24 * 60 * 60
        with self._lock, self._connect_monitor() as db:
            history = db.execute(
                """
                SELECT observed_at, total_tokens, session_count, active_count
                FROM overview_samples
                WHERE observed_at >= ? AND account_key = ?
                ORDER BY observed_at
                """,
                (history_cutoff, self.collector_account_key()),
            ).fetchall()
            previous = {}
            for session in all_sessions:
                row = db.execute("""SELECT tokens_used FROM session_samples
                    WHERE account_key=? AND node_key=? AND session_key=? AND observed_at<?
                    ORDER BY observed_at DESC LIMIT 1""",
                    (self.collector_account_key(), session["node_key"], session["key"], now - self.config.interval)).fetchone()
                if row is not None:
                    previous[(session["node_key"], session["key"])] = row["tokens_used"]

        for session in sessions:
            session["active"] = now - session["updated_at"] <= self.config.active_window
            session["delta"] = max(0, session["tokens"] - previous.get(
                (session["node_key"], session["key"]), session["tokens"],
            ))

        session_total = sum(session["tokens"] for session in sessions)
        account_usage = self.read_account_usage()
        lifetime_tokens = (account_usage or {}).get("summary", {}).get("lifetime_tokens")
        aliases = self.read_aliases()
        for node in nodes:
            node["display_name"] = aliases.get(node["key"], node["os_user"])
        daily_user_trend = self.read_daily_user_trend(nodes, now)
        names = {node["key"]: node["display_name"] for node in nodes}
        for session in sessions:
            session["display_user"] = names.get(session["node_key"], session["os_user"])
        weekly_usage = self.read_weekly_usage_history(account_usage, now)
        weekly_attribution = {}
        if weekly_usage:
            weekly_attribution = self.read_period_attribution(
                all_sessions, nodes, UsageWindow.weekly(weekly_usage, now),
            )
        for session in sessions:
            usage = weekly_attribution.get((session["node_key"], session["key"]))
            session["weekly_tokens"] = usage.tokens if usage is not None else None
            session["verified_weekly_tokens"] = usage.event_tokens if usage is not None else None
            session["weekly_snapshot_only_tokens"] = usage.snapshot_only_tokens if usage is not None else None
        reconciliation = self.read_usage_reconciliation(
            all_sessions, nodes, account_usage, weekly_usage, now, weekly_attribution if weekly_usage else None,
        )
        weekly_token_breakdown = self.read_weekly_token_breakdown(
            account_usage, weekly_usage, nodes, reconciliation, now, weekly_attribution,
        )
        weekly_account_tokens = (
            weekly_token_breakdown["account_tokens"] if weekly_token_breakdown is not None else None
        )
        with self._lock, self._connect_monitor() as db:
            analytics = read_analytics(db, account_usage, weekly_usage, all_sessions, nodes, now, weekly_token_breakdown, account_key=self.collector_account_key())
            weekly_quota = read_quota_usage(db, self.collector_account_key(), weekly_usage,
                                           aggregate_users(weekly_attribution), now, account_usage)
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
            "accounts": self.accounts(),
            "selected_account_key": self.collector_account_key(),
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
            "weekly_quota": weekly_quota,
            "weekly_token_breakdown": weekly_token_breakdown,
            "weekly_user_tokens": aggregate_users(weekly_attribution) if weekly_usage else None,
            "daily_user_trend": daily_user_trend,
            "analytics": analytics,
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
                local_key = self.store.read_account(self.store.collector_token)["key"]
                if not any(row["account_key"] == local_key and row["enabled"] for row in self.store.accounts()["registered"]):
                    self.account_usage_error = None
                    self.stopped.wait(self.store.config.interval)
                    continue
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

        def end_headers(self) -> None:
            # HTML and scripts must revalidate together after a deployment,
            # including conditional 304 responses for unchanged assets.
            if not urlparse(self.path).path.startswith("/api/"):
                self.send_header("Cache-Control", "no-cache, max-age=0, must-revalidate")
            super().end_headers()

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
            return hmac.compare_digest(username.encode(), store.dashboard_user.encode()) and hmac.compare_digest(
                password.encode(), store.dashboard_password.encode()
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
                    account_key = parse_qs(urlparse(self.path).query).get("account_key", [None])[0]
                    self.send_json(store.dashboard(account_key=account_key))
                except ValueError as error:
                    self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                except (OSError, sqlite3.Error) as error:
                    self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if path == "/api/accounts":
                self.send_json(store.accounts())
                return
            if path == "/":
                self.path = "/index.html"
            super().do_GET()

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/accounts":
                if not self.dashboard_authorized():
                    self.request_dashboard_login()
                    return
                origin = self.headers.get("Origin")
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json" or (
                    origin and urlparse(origin).netloc != self.headers.get("Host")
                ):
                    self.send_json({"error": "invalid account request origin or content type"}, HTTPStatus.FORBIDDEN)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 4096:
                        raise ValueError("invalid payload size")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict):
                        raise ValueError("invalid account payload")
                    store.save_account(payload.get("account_key"), payload.get("label"),
                                       payload.get("enabled", True), payload.get("make_default", False))
                    self.send_json({"ok": True, "accounts": store.accounts()})
                except (ValueError, TypeError) as error:
                    self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            if path not in ("/api/ingest", "/api/collector/config"):
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
                if path == "/api/collector/config":
                    response = store.discover_account(payload)
                    response.update({"ok": True, "server_version": COLLECTOR_VERSION})
                    update = store._collector_update(payload.get("collector"))
                    if update:
                        response["update"] = update
                    self.send_json(response, response_token=store.collector_token)
                    return
                store.ingest(payload)
                store.collect()
                node_key = str((payload.get("node") or {}).get("key") or "")
                with store.account_scope(payload["account"]["key"]):
                    response = store.collector_response(node_key, payload.get("collector"))
                self.send_json(
                    response, HTTPStatus.ACCEPTED, response_token=store.collector_token,
                )
            except PermissionError as error:
                self.send_json({"error": str(error)}, HTTPStatus.UNAUTHORIZED)
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except (OSError, sqlite3.Error) as error:
                self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)

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
                if not isinstance(payload, dict):
                    raise ValueError("invalid alias payload")
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
