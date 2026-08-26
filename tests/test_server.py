import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from server import Config, UsageStore


class UsageStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "state.sqlite"
        self.monitor = root / "monitor.sqlite"
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute(
                """CREATE TABLE threads (
                    id TEXT, cwd TEXT, model TEXT, tokens_used INTEGER,
                    created_at INTEGER, updated_at INTEGER, archived INTEGER,
                    title TEXT, preview TEXT, rollout_path TEXT
                )"""
            )
            db.executemany(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("raw-session-a", "/private/project-a", "gpt-test", 1200, 800, 990, 0, "secret title", "secret preview", "/secret"),
                    ("raw-session-b", "/private/project-b", "gpt-test", 300, 700, 800, 1, "archived", "private", "/secret2"),
                ],
            )
        self.store = UsageStore(Config(
            self.source, self.monitor, "test-salt",
            auth_file=root / "missing-auth.json",
            collector_token_file=root / "collector.token",
            dashboard_password_file=root / "dashboard.password",
            interval=10, active_window=60,
        ))

    def tearDown(self):
        self.temp.cleanup()

    def test_dashboard_excludes_private_fields_and_archived_sessions(self):
        self.store.collect(now=1000)
        payload = self.store.dashboard(now=1000)
        serialized = str(payload)
        self.assertEqual(payload["summary"]["session_count"], 1)
        self.assertEqual(payload["summary"]["total_tokens"], 1200)
        self.assertTrue(payload["sessions"][0]["active"])
        for private_value in ["raw-session-a", "/private/project-a", "secret title", "secret preview", "/secret"]:
            self.assertNotIn(private_value, serialized)

    def test_collect_records_token_growth(self):
        self.store.collect(now=1000)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE threads SET tokens_used = 1500, updated_at = 1010 WHERE id = 'raw-session-a'")
        self.store.collect(now=1010)
        payload = self.store.dashboard(now=1011)
        self.assertEqual(payload["sessions"][0]["tokens"], 1500)
        self.assertEqual(payload["sessions"][0]["delta"], 300)
        self.assertEqual(len(payload["history"]), 2)

    def test_ingest_adds_remote_node_without_raw_content(self):
        payload = {
            "node": {"key": "remote-node", "hostname": "worker-1", "os_user": "alice"},
            "account": {"key": "acct", "name": "Alice", "email": "alice@example.com", "plan": "plus"},
            "sessions": [{
                "key": "hashed-session", "project_key": "hashed-project", "project_name": "frontend",
                "model": "gpt-test", "tokens": 900, "created_at": 900, "updated_at": 995, "archived": False,
            }],
        }
        self.store.ingest(payload, now=1000)
        dashboard = self.store.dashboard(now=1000)
        remote = next(session for session in dashboard["sessions"] if session["source"] == "collector")
        self.assertEqual(remote["project_name"], "frontend")
        self.assertEqual(remote["hostname"], "worker-1")
        self.assertTrue(any(node["account"].get("email") == "alice@example.com" for node in dashboard["nodes"]))

    def test_account_usage_is_canonical_and_not_summed_across_nodes(self):
        def report(node_key, hostname, lifetime, used_percent):
            return {
                "node": {"key": node_key, "hostname": hostname, "os_user": "alice"},
                "account": {"key": "shared-acct", "email": "shared@example.com"},
                "sessions": [],
                "account_usage": {
                    "source": "codex-app-server", "observed_at": 1000,
                    "summary": {
                        "lifetime_tokens": lifetime, "peak_daily_tokens": 800,
                        "longest_running_turn_seconds": 30, "current_streak_days": 2,
                        "longest_streak_days": 5,
                    },
                    "daily_usage": [{"date": "2026-08-24", "tokens": 400}],
                    "rate_limits": [{
                        "id": "codex", "name": "codex", "primary": {
                            "used_percent": used_percent, "window_minutes": 300, "resets_at": 2000,
                        }, "secondary": None,
                    }],
                },
            }

        self.store.ingest(report("node-a", "worker-a", 5000, 20), now=1000)
        self.store.ingest(report("node-b", "worker-b", 5000, 25), now=1010)
        dashboard = self.store.dashboard(now=1010)
        self.assertEqual(dashboard["summary"]["total_tokens"], 5000)
        self.assertEqual(dashboard["summary"]["lifetime_tokens"], 5000)
        self.assertEqual(dashboard["summary"]["session_tokens"], 1200)
        self.assertEqual(dashboard["account_usage"]["rate_limits"][0]["primary"]["used_percent"], 25)
        self.assertEqual(dashboard["account_usage"]["reported_by"]["hostname"], "worker-b")

        # A delayed lower account-wide total must not overwrite the canonical snapshot.
        self.store.ingest(report("node-a", "worker-a", 4900, 10), now=1020)
        after_stale = self.store.dashboard(now=1020)
        self.assertEqual(after_stale["summary"]["total_tokens"], 5000)
        self.assertEqual(after_stale["summary"]["lifetime_tokens"], 5000)
        self.assertEqual(after_stale["account_usage"]["reported_by"]["hostname"], "worker-b")

    def test_reconciliation_finds_unattributed_usage_and_compares_nodes(self):
        def report(tokens, lifetime):
            return {
                "node": {"key": "node-a", "hostname": "worker-a", "os_user": "alice"},
                "account": {"key": "shared-acct"},
                "sessions": [{
                    "key": "session-a", "project_key": "project-a", "project_name": "frontend",
                    "model": "gpt-test", "tokens": tokens, "created_at": 900_000,
                    "updated_at": 1_000_000, "archived": False,
                }],
                "account_usage": {
                    "source": "codex-app-server",
                    "summary": {"lifetime_tokens": lifetime}, "daily_usage": [],
                    "rate_limits": [{
                        "id": "codex", "name": "codex", "primary": {
                            "used_percent": 20, "window_minutes": 10080, "resets_at": 1_500_000,
                        }, "secondary": None,
                    }],
                },
            }

        with patch.object(self.store, "read_sessions", return_value=[]):
            self.store.ingest(report(100, 1000), now=1_000_000)
            self.store.collect(now=1_000_000)
            self.store.ingest(report(300, 1300), now=1_000_030)
            self.store.collect(now=1_000_030)
            dashboard = self.store.dashboard(now=1_000_030)

        recon = dashboard["reconciliation"]
        self.assertEqual(recon["period"]["account_tokens"], 300)
        self.assertEqual(recon["period"]["attributed_session_tokens"], 200)
        self.assertEqual(recon["period"]["difference_tokens"], 100)
        self.assertEqual(dashboard["summary"]["total_tokens"], 1300)
        worker = next(item for item in recon["node_comparison"] if item["node_key"] == "node-a")
        self.assertEqual(worker["tokens"], 300)
        self.assertEqual(worker["attributed_total_tokens"], 300)
        self.assertFalse(worker["partial"])
        self.assertEqual(worker["status"], "high")

    def test_node_comparison_shows_range_for_usage_before_first_observation(self):
        def report(tokens, lifetime):
            return {
                "node": {"key": "node-a", "hostname": "worker-a", "os_user": "alice"},
                "account": {"key": "shared-acct"},
                "sessions": [{
                    "key": "old-session", "project_key": "project-a", "project_name": "frontend",
                    "model": "gpt-test", "tokens": tokens, "created_at": 800_000,
                    "updated_at": 1_000_030, "archived": False,
                }],
                "account_usage": {
                    "source": "codex-app-server", "summary": {"lifetime_tokens": lifetime},
                    "daily_usage": [], "rate_limits": [{
                        "id": "codex", "name": "codex", "primary": {
                            "used_percent": 20, "window_minutes": 10080, "resets_at": 1_500_000,
                        }, "secondary": None,
                    }],
                },
            }

        with patch.object(self.store, "read_sessions", return_value=[]):
            self.store.ingest(report(100, 1000), now=1_000_000)
            self.store.collect(now=1_000_000)
            self.store.ingest(report(300, 1300), now=1_000_030)
            self.store.collect(now=1_000_030)
            dashboard = self.store.dashboard(now=1_000_030)

        worker = next(
            item for item in dashboard["reconciliation"]["node_comparison"]
            if item["node_key"] == "node-a"
        )
        self.assertEqual(worker["tokens"], 200)
        self.assertEqual(worker["maximum_tokens"], 300)
        self.assertEqual(worker["attributed_total_tokens"], 300)
        self.assertTrue(worker["partial"])
        self.assertEqual(worker["partial_session_count"], 1)

    def test_verified_event_buckets_and_targeted_pace_alert(self):
        report = {
            "collector": {"version": "0.6.0", "kind": "user"},
            "node": {"key": "node-a", "hostname": "worker-a", "os_user": "alice"},
            "account": {"key": "shared-acct"},
            "sessions": [{
                "key": "session-a", "project_key": "project-a", "project_name": "frontend",
                "model": "gpt-test", "tokens": 500, "created_at": 900_000,
                "updated_at": 1_000_000, "archived": False,
                "usage_buckets": [{"at": 990_000, "tokens": 250}],
            }],
            "account_usage": {
                "source": "codex-app-server", "summary": {"lifetime_tokens": 5000},
                "daily_usage": [], "rate_limits": [{
                    "id": "codex", "name": "codex", "primary": {
                        "used_percent": 30, "window_minutes": 10080, "resets_at": 1_500_000,
                    }, "secondary": None,
                }],
            },
        }
        with patch.object(self.store, "read_sessions", return_value=[]):
            self.store.ingest(report, now=1_000_000)
            self.store.collect(now=1_000_000)
            dashboard = self.store.dashboard(now=1_000_000)
            with patch("server.COLLECTOR_VERSION", "0.7.0"):
                response = self.store.collector_response("node-a", report["collector"], now=1_000_000)
            report["alert_receipts"] = [response["alerts"][0]["id"]]
            self.store.ingest(report, now=1_000_001)
            repeated = self.store.collector_response("node-a", report["collector"], now=1_000_001)

        worker = next(
            item for item in dashboard["reconciliation"]["node_comparison"]
            if item["node_key"] == "node-a"
        )
        self.assertEqual(worker["verified_event_tokens"], 250)
        self.assertEqual(response["update"]["version"], "0.7.0")
        self.assertEqual(response["alerts"][0]["kind"], "weekly-pace-150")
        self.assertNotIn("alerts", repeated)

    def test_weekly_usage_history_starts_over_without_resetting_total_processed(self):
        def report(used_percent, resets_at, lifetime=5000):
            return {
                "node": {"key": "node-a", "hostname": "worker-a", "os_user": "alice"},
                "account": {"key": "shared-acct"}, "sessions": [],
                "account_usage": {
                    "source": "codex-app-server",
                    "summary": {"lifetime_tokens": lifetime}, "daily_usage": [],
                    "rate_limits": [{
                        "id": "codex", "name": "codex", "primary": {
                            "used_percent": used_percent, "window_minutes": 10080,
                            "resets_at": resets_at,
                        }, "secondary": None,
                    }],
                },
            }

        self.store.ingest(report(80, 1_100_000), now=1_000_000)
        before = self.store.dashboard(now=1_000_000)["weekly_usage"]
        self.assertEqual(before["used_percent"], 80)
        self.assertEqual(before["points"][-1]["used_percent"], 80)

        self.store.ingest(report(0, 1_704_800), now=1_100_010)
        after = self.store.dashboard(now=1_100_010)["weekly_usage"]
        self.assertEqual(after["window_started_at"], "1970-01-13T17:33:20Z")
        self.assertEqual([point["used_percent"] for point in after["points"]], [0.0, 0.0])
        self.assertNotIn(80, [point["used_percent"] for point in after["points"]])
        self.assertEqual(self.store.dashboard(now=1_100_010)["summary"]["total_tokens"], 5000)

        self.store.ingest(report(2, 1_704_800, lifetime=5200), now=1_100_040)
        current = self.store.dashboard(now=1_100_040)
        self.assertEqual(current["summary"]["total_tokens"], 5200)
        self.assertEqual(current["summary"]["lifetime_tokens"], 5200)

    def test_ingest_rejects_an_unregistered_account(self):
        payload = {
            "node": {"key": "remote-node", "hostname": "worker-1", "os_user": "alice"},
            "account": {"key": "different-account"},
            "sessions": [],
        }
        with patch.object(self.store, "collector_account_key", return_value="registered-account"):
            with self.assertRaisesRegex(ValueError, "not registered"):
                self.store.ingest(payload, now=1000)

    def test_dashboard_hides_previously_stored_unregistered_accounts(self):
        payload = {
            "node": {"key": "old-node", "hostname": "worker-1", "os_user": "bob"},
            "account": {"key": "old-account"},
            "sessions": [{
                "key": "old-session", "project_key": "old-project", "project_name": "private",
                "model": "gpt-test", "tokens": 500, "created_at": 900, "updated_at": 995,
                "archived": False,
            }],
        }
        self.store.ingest(payload, now=1000)
        with patch.object(self.store, "collector_account_key", return_value="registered-account"):
            dashboard = self.store.dashboard(now=1000)
        self.assertFalse(any(node["key"] == "old-node" for node in dashboard["nodes"]))
        self.assertFalse(any(session["key"] == "old-session" for session in dashboard["sessions"]))

    def test_node_alias_persists_separately_from_os_user(self):
        node_key = self.store.local_node_key()
        self.store.set_node_alias(node_key, "Backend Team")
        dashboard = self.store.dashboard(now=1000)
        self.assertEqual(dashboard["nodes"][0]["display_name"], "Backend Team")
        self.assertEqual(dashboard["nodes"][0]["os_user"], __import__("getpass").getuser())
        self.assertTrue(all(session["display_user"] == "Backend Team" for session in dashboard["sessions"]))
        self.store.set_node_alias(node_key, "")
        reset = self.store.dashboard(now=1000)
        self.assertEqual(reset["nodes"][0]["display_name"], __import__("getpass").getuser())


if __name__ == "__main__":
    unittest.main()
