"""Isolated dashboard data for the optional headless browser check."""
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Collector, Config, UsageStore, build_handler

with tempfile.TemporaryDirectory(prefix="codex-account-browser-") as directory:
    root = Path(directory)
    (root / "password").write_text("test-password")
    (root / "token").write_text("fixture-collector-token")
    store = UsageStore(Config(root / "missing-db", root / "monitor.sqlite", "fixture",
                             auth_file=root / "missing-auth", collector_token_file=root / "token",
                             dashboard_password_file=root / "password"))
    now = int(time.time())
    for key, name, tokens, lifetime in (("aaaaaaaaaaaa", "Account A", 100, 1000), ("bbbbbbbbbbbb", "Account B", 700, 9000)):
        store.save_account(key, name, make_default=key == "aaaaaaaaaaaa")
        if key == "aaaaaaaaaaaa":
            store.ingest({"node": {"key": "same-node", "hostname": "worker", "os_user": "user"},
                          "account": {"key": key}, "sessions": [],
                          "account_usage": {"source": "codex-app-server", "summary": {"lifetime_tokens": 0},
                                            "rate_limits": [{"id": "codex", "primary": {
                                                "used_percent": 5, "window_minutes": 10080,
                                                "resets_at": now + 6 * 86400}}]}}, now=now - 90)
        store.ingest({"node": {"key": "same-node", "hostname": "worker", "os_user": "user"},
                      "collector": {"version": "0.7.1" if key == "aaaaaaaaaaaa" else "0.7.0"},
                      "account": {"key": key, "email": name + "@example.test"},
                      "sessions": [{"key": "session", "project_key": "project", "tokens": tokens,
                                    "created_at": now - 300, "updated_at": now - 120,
                                    "usage_detail_backfill_complete": True,
                                    "usage_details": [{"at": now // 60 * 60 - 60, "model": "small-model", "effort": "low",
                                        "service_tier": "standard", "reasoning_mode": "standard", "tokens": tokens,
                                        "events": 1, "input_tokens": tokens - 5, "cached_input_tokens": tokens - 10,
                                        "output_tokens": 5, "reasoning_output_tokens": 2}] if key == "aaaaaaaaaaaa" else [],
                                    "usage_buckets": [{"at": now // 60 * 60 - 60, "tokens": tokens}]}],
                      "account_usage": {"source": "codex-app-server", "summary": {"lifetime_tokens": lifetime},
                                        "daily_usage": [{"date": datetime.now(timezone.utc).date().isoformat(), "tokens": lifetime}],
                                        "rate_limits": [{"id": "codex", "primary": {"used_percent": 20 if key == "aaaaaaaaaaaa" else 3.125,
                                            "window_minutes": 10080, "resets_at": now + 6 * 86400}}]}}, now=now)
    store.discover_account({"account": {"key": "cccccccccccc", "email": "candidate@example.test"}})
    store.collect(now=now)
    http = ThreadingHTTPServer(("127.0.0.1", 18766), build_handler(store, Collector(store)))
    timeout = threading.Timer(600, http.shutdown)
    timeout.daemon = True
    timeout.start()
    print("Account browser fixture: http://127.0.0.1:18766", flush=True)
    try:
        http.serve_forever()
    finally:
        timeout.cancel()
        http.server_close()
