import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from collector import read_sessions, snapshot
from collector_windows_machine import discover_profiles, profile_snapshot


class CollectorTest(unittest.TestCase):
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
            alice.mkdir(parents=True)
            public.mkdir(parents=True)
            empty.mkdir()
            (alice / "state_5.sqlite").touch()
            (public / "state_5.sqlite").touch()
            self.assertEqual(discover_profiles(root), [root / "alice"])


if __name__ == "__main__":
    unittest.main()
