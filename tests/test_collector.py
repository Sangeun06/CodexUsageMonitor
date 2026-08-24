import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from collector import append_log, attribute_sessions, read_sessions, snapshot
from collector_windows_machine import discover_profiles, profile_snapshot


class CollectorTest(unittest.TestCase):
    def test_log_rotation_respects_size_and_backup_count(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "collector.log"
            append_log(log, "first-line", 16, 2)
            append_log(log, "second-line", 16, 2)
            append_log(log, "third-line", 16, 2)
            self.assertEqual(log.read_text(), "third-line\n")
            self.assertEqual(log.with_name("collector.log.1").read_text(), "second-line\n")
            self.assertEqual(log.with_name("collector.log.2").read_text(), "first-line\n")

    def test_session_attribution_survives_account_switches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth.json"
            state = root / "collector-state.json"
            auth.write_text("{}")
            os.utime(auth, (100, 100))
            old = {"key": "old", "created_at": 80}
            shared = {"key": "shared", "created_at": 110}
            other = {"key": "other", "created_at": 120}

            first = attribute_sessions([old, shared], "account-a", auth, state, "node")
            self.assertEqual([item["key"] for item in first], ["shared"])
            second = attribute_sessions([old, shared, other], "account-b", auth, state, "node")
            self.assertEqual([item["key"] for item in second], ["other"])
            switched_back = attribute_sessions([old, shared, other], "account-a", auth, state, "node")
            self.assertEqual([item["key"] for item in switched_back], ["shared"])

    def test_machine_collector_supports_embedded_python_imports(self):
        source = (Path(__file__).resolve().parents[1] / "collector_windows_machine.py").read_text()
        path_setup = source.index("sys.path.insert")
        collector_import = source.index("from collector import")
        self.assertLess(path_setup, collector_import)

    def test_snapshot_excludes_content_and_full_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.sqlite"
            auth = root / "auth.json"
            auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {}}))
            with sqlite3.connect(state) as db:
                db.execute(
                    """CREATE TABLE threads (
                        id TEXT, cwd TEXT, model TEXT, tokens_used INTEGER,
                        created_at INTEGER, updated_at INTEGER, archived INTEGER,
                        title TEXT, preview TEXT
                    )"""
                )
                db.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("raw-id", "/home/alice/private/project", "gpt-test", 42, 1, 2, 0, "secret title", "secret body"),
                )
            payload = snapshot(state, auth, "shared-token")
            encoded = json.dumps(payload)
            self.assertEqual(payload["sessions"][0]["project_name"], "project")
            for secret in ("raw-id", "/home/alice", "secret title", "secret body"):
                self.assertNotIn(secret, encoded)

    def test_machine_collector_discovers_only_codex_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alice = root / "alice" / ".codex"
            public = root / "Public" / ".codex"
            empty = root / "bob"
            fresh_login = root / "charlie" / ".codex"
            alice.mkdir(parents=True)
            public.mkdir(parents=True)
            empty.mkdir()
            fresh_login.mkdir(parents=True)
            (alice / "state_5.sqlite").touch()
            (public / "state_5.sqlite").touch()
            (fresh_login / "auth.json").write_text("{}")
            self.assertEqual(discover_profiles(root), [root / "alice", root / "charlie"])

    def test_machine_snapshot_allows_login_before_first_session(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "alice"
            codex_dir = profile / ".codex"
            codex_dir.mkdir(parents=True)
            (codex_dir / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {}}))
            payload = profile_snapshot(profile, "shared-token")
            self.assertEqual(payload["sessions"], [])
            self.assertEqual(payload["node"]["os_user"], "alice")


if __name__ == "__main__":
    unittest.main()
