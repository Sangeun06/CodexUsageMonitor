import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import Config, UsageStore


class UsageStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "state.sqlite"
        self.monitor = root / "monitor.sqlite"
        with sqlite3.connect(self.source) as db:
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
        with sqlite3.connect(self.source) as db:
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
