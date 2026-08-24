#!/usr/bin/env python3
"""Privacy-first local Codex token usage dashboard."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


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

    def _connect_monitor(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.config.monitor_db, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _connect_source(self) -> sqlite3.Connection:
        if not self.config.source_db.is_file():
            raise FileNotFoundError(f"Codex database not found: {self.config.source_db}")
        uri = f"file:{self.config.source_db}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

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
                """
            )

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
        clean_sessions = []
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

    def read_remote(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        local_key = self.local_node_key()
        with self._lock, self._connect_monitor() as db:
            nodes = db.execute(
                "SELECT node_key, hostname, os_user, account_json, seen_at FROM collector_nodes WHERE node_key != ?",
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
        with self._lock, self._connect_monitor() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO overview_samples
                    (observed_at, total_tokens, session_count, active_count)
                VALUES (?, ?, ?, ?)
                """,
                (now, sum(session["tokens"] for session in visible), len(visible), active),
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
        remote_nodes, remote_sessions = self.read_remote()
        sessions = [session for session in self.read_sessions() + remote_sessions if not session["archived"]]
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
            session["created_at"] = utc_iso(session["created_at"])
            session["updated_at"] = utc_iso(session["updated_at"])

        total = sum(session["tokens"] for session in sessions)
        local_node = {
            "key": self.local_node_key(),
            "hostname": socket.gethostname(),
            "os_user": getpass.getuser(),
            "seen_at": now,
            "account": self.read_account(),
        }
        nodes = [local_node] + remote_nodes
        aliases = self.read_aliases()
        for node in nodes:
            node["display_name"] = aliases.get(node["key"], node["os_user"])
        names = {node["key"]: node["display_name"] for node in nodes}
        for session in sessions:
            session["display_user"] = names.get(session["node_key"], session["os_user"])
        return {
            "summary": {
                "total_tokens": total,
                "session_count": len(sessions),
                "active_count": sum(session["active"] for session in sessions),
                "project_count": len({session["project_key"] for session in sessions}),
                "model_count": len({session["model"] for session in sessions}),
            },
            "sessions": sessions,
            "nodes": nodes,
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

    def run(self) -> None:
        while not self.stopped.is_set():
            try:
                self.store.collect()
                self.last_error = None
            except (OSError, sqlite3.Error) as error:
                self.last_error = str(error)
            self.stopped.wait(self.store.config.interval)


def build_handler(store: UsageStore, collector: Collector):
    class DashboardHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[{self.log_date_time_string()}] {format % args}")

        def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
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
                self.send_json({"ok": collector.last_error is None, "error": collector.last_error})
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
                store.ingest(json.loads(body))
                store.collect()
                self.send_json({"ok": True}, HTTPStatus.ACCEPTED)
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
