import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from pathlib import PureWindowsPath
from unittest.mock import patch

from collector import COLLECTOR_VERSION, _read_rollout_usage_buckets, append_log, apply_collector_update, normalize_app_server_snapshot, read_sessions, snapshot, track_target_usage
from collector_windows_machine import discover_profiles, profile_snapshot, profiles_root_for_drive


class CollectorTest(unittest.TestCase):
    def test_rollout_parser_skips_non_object_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            rollout.write_text('null\n[]\n42\n' + json.dumps({
                "type": "event_msg", "timestamp": "1970-01-01T00:17:00Z",
                "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"total_tokens": 40},
                }},
            }) + '\n')
            buckets, offset, _ = _read_rollout_usage_buckets(rollout, 0, 0)
            self.assertEqual(buckets, [{"at": 1020, "tokens": 40}])
            self.assertEqual(offset, rollout.stat().st_size)

    def test_repeated_cumulative_rollout_usage_is_counted_once_across_scans(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            def event(total, last):
                return json.dumps({
                    "type": "event_msg", "timestamp": "1970-01-01T00:17:00Z",
                    "payload": {"type": "token_count", "info": {
                        "total_token_usage": {"total_tokens": total},
                        "last_token_usage": {"total_tokens": last},
                    }},
                }) + '\n'
            rollout.write_text(event(100, 40) + event(100, 40))
            buckets, offset, total = _read_rollout_usage_buckets(rollout, 0, 0)
            self.assertEqual(buckets, [{"at": 1020, "tokens": 40}])
            with rollout.open('a') as stream:
                stream.write(event(100, 40) + event(150, 50))
            buckets, _, _ = _read_rollout_usage_buckets(rollout, 0, offset, total)
            self.assertEqual(buckets, [{"at": 1020, "tokens": 50}])

    def test_usage_growth_during_an_observation_gap_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth, state = root / "auth.json", root / "state.json"
            auth.write_text("{}")
            os.utime(auth, (800, 800))
            session = {"key": "session", "created_at": 900, "updated_at": 1000, "tokens": 100}
            with patch("collector.time.time", return_value=1000):
                track_target_usage([session], "target", "target", auth, state, "node")
            session.update(tokens=300, updated_at=2000)
            with patch("collector.time.time", return_value=2000):
                resumed = track_target_usage([session], "target", "target", auth, state, "node")
            self.assertEqual(resumed[0]["tokens"], 100)
            session.update(tokens=350, updated_at=2030)
            with patch("collector.time.time", return_value=2030):
                continuous = track_target_usage([session], "target", "target", auth, state, "node")
            self.assertEqual(continuous[0]["tokens"], 150)

    def test_new_session_during_an_observation_gap_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth, state = root / "auth.json", root / "state.json"
            auth.write_text("{}")
            with patch("collector.time.time", return_value=1000):
                track_target_usage([], "target", "target", auth, state, "node")
            session = {"key": "session", "created_at": 1100, "updated_at": 2000, "tokens": 100}
            with patch("collector.time.time", return_value=2000):
                resumed = track_target_usage([session], "target", "target", auth, state, "node")
            self.assertEqual(resumed, [])
            session.update(tokens=120, updated_at=2030)
            with patch("collector.time.time", return_value=2030):
                continuous = track_target_usage([session], "target", "target", auth, state, "node")
            self.assertEqual(continuous[0]["tokens"], 20)

    def test_newly_observed_session_before_a_target_login_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth, state = root / "auth.json", root / "state.json"
            auth.write_text("{}")
            with patch("collector.time.time", return_value=1000):
                track_target_usage([], "other", "target", auth, state, "node")
            os.utime(auth, (1050, 1050))
            session = {"key": "session", "created_at": 1020, "updated_at": 1040, "tokens": 100}
            with patch("collector.time.time", return_value=1100):
                switched = track_target_usage([session], "target", "target", auth, state, "node")
            self.assertEqual(switched, [])
            session.update(tokens=125, updated_at=1130)
            with patch("collector.time.time", return_value=1130):
                continuous = track_target_usage([session], "target", "target", auth, state, "node")
            self.assertEqual(continuous[0]["tokens"], 25)

    def test_failed_update_validation_keeps_current_file_and_existing_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "collector.py"
            backup = root / "collector.py.previous"
            target.write_text("CURRENT = True\n")
            backup.write_text("PREVIOUS = True\n")
            content = b"NEW = True\n"
            update = {"version": "9.0.0", "files": {
                "collector.py": {"sha256": hashlib.sha256(content).hexdigest(),
                                 "content": base64.b64encode(content).decode()},
                "collector_windows_machine.py": {"sha256": "invalid", "content": "eA=="},
            }}
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                apply_collector_update(update, root)
            self.assertEqual(target.read_text(), "CURRENT = True\n")
            self.assertEqual(backup.read_text(), "PREVIOUS = True\n")
            self.assertFalse((root / "collector.py.update").exists())

    def test_verified_rollout_buckets_are_sent_only_after_continuous_target_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth.json"
            state = root / "collector-state.json"
            rollout = root / "rollout.jsonl"
            auth.write_text("{}")
            os.utime(auth, (800, 800))
            rollout.write_text("")
            session = {
                "key": "session", "created_at": 900, "updated_at": 1070, "tokens": 100,
                "_rollout_path": str(rollout),
            }
            with patch("collector.time.time", return_value=1000):
                first = track_target_usage([session], "target", "target", auth, state, "node")
            self.assertNotIn("usage_buckets", first[0])

            rollout.write_text(json.dumps({
                "type": "event_msg", "timestamp": "1970-01-01T00:17:00Z",
                "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"total_tokens": 40},
                }},
            }) + "\n")
            session["tokens"] = 140
            with patch("collector.time.time", return_value=1120):
                second = track_target_usage([session], "target", "target", auth, state, "node")
            self.assertEqual(second[0]["usage_buckets"], [{"at": 1020, "tokens": 40}])

    def test_current_minute_rollout_bucket_waits_for_account_continuity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth.json"
            state = root / "collector-state.json"
            rollout = root / "rollout.jsonl"
            auth.write_text("{}")
            rollout.write_text("")
            session = {
                "key": "session", "created_at": 900, "updated_at": 1040, "tokens": 100,
                "_rollout_path": str(rollout),
            }
            with patch("collector.time.time", return_value=1000):
                track_target_usage([session], "target", "target", auth, state, "node")

            rollout.write_text(json.dumps({
                "type": "event_msg", "timestamp": "1970-01-01T00:17:10Z",
                "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"total_tokens": 25},
                }},
            }) + "\n")
            session["tokens"] = 125
            with patch("collector.time.time", return_value=1045):
                pending = track_target_usage([session], "target", "target", auth, state, "node")
            self.assertNotIn("usage_buckets", pending[0])

            with patch("collector.time.time", return_value=1080):
                confirmed = track_target_usage([session], "target", "target", auth, state, "node")
            self.assertEqual(confirmed[0]["usage_buckets"], [{"at": 1020, "tokens": 25}])

    def test_atomic_collector_update_checks_checksum_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "collector.py"
            target.write_text("OLD = True\n")
            content = b"NEW = True\n"
            update = {
                "version": "9.0.0",
                "files": {"collector.py": {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content": base64.b64encode(content).decode(),
                }},
            }
            self.assertTrue(apply_collector_update(update, root))
            self.assertEqual(target.read_bytes(), content)
            self.assertEqual((root / "collector.py.previous").read_text(), "OLD = True\n")
            self.assertEqual(COLLECTOR_VERSION, "0.7.0")

    def test_app_server_snapshot_keeps_only_aggregate_usage(self):
        result = normalize_app_server_snapshot(
            {"account": {"type": "chatgpt", "email": "shared@example.com", "planType": "pro"}},
            {
                "summary": {
                    "lifetimeTokens": 1234, "peakDailyTokens": 300,
                    "longestRunningTurnSec": 45, "currentStreakDays": 2,
                    "longestStreakDays": 7, "private": "do-not-copy",
                },
                "dailyUsageBuckets": [
                    {"startDate": "2026-08-24", "tokens": 50, "messages": "secret"},
                    {"startDate": "not-a-date", "tokens": 999},
                ],
            },
            {"rateLimits": {
                "limitId": "codex", "primary": {
                    "usedPercent": 25.5, "windowDurationMins": 300,
                    "resetsAt": 2000, "credits": "private",
                },
            }},
            observed_at=1000,
        )
        usage = result["usage"]
        self.assertEqual(usage["summary"]["lifetime_tokens"], 1234)
        self.assertEqual(usage["daily_usage"], [{"date": "2026-08-24", "tokens": 50}])
        self.assertEqual(usage["rate_limits"][0]["primary"]["used_percent"], 25.5)
        serialized = json.dumps(result)
        for excluded in ("do-not-copy", "secret", "credits", "messages"):
            self.assertNotIn(excluded, serialized)

    def test_windows_profiles_root_is_an_absolute_drive_path(self):
        root = PureWindowsPath(str(profiles_root_for_drive("C:")))
        self.assertTrue(root.is_absolute())
        self.assertEqual(root, PureWindowsPath("C:/Users"))

    def test_log_rotation_respects_size_and_backup_count(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "collector.log"
            append_log(log, "first-line", 16, 2)
            append_log(log, "second-line", 16, 2)
            append_log(log, "third-line", 16, 2)
            self.assertEqual(log.read_text(), "third-line\n")
            self.assertEqual(log.with_name("collector.log.1").read_text(), "second-line\n")
            self.assertEqual(log.with_name("collector.log.2").read_text(), "first-line\n")

    def test_target_usage_accumulates_only_during_target_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth.json"
            state = root / "collector-state.json"
            auth.write_text("{}")
            os.utime(auth, (100, 100))
            old = {"key": "old", "created_at": 80, "updated_at": 80, "tokens": 50}
            shared = {"key": "shared", "created_at": 110, "updated_at": 110, "tokens": 100}

            first = track_target_usage([old, shared], "account-a", "account-a", auth, state, "node")
            self.assertEqual([(item["key"], item["tokens"]) for item in first], [("shared", 100)])

            # Usage immediately before switching away is credited to the
            # previous target login, even if the next poll sees account-b.
            shared.update(tokens=120, updated_at=190)
            os.utime(auth, (200, 200))
            after_switch = track_target_usage(
                [old, shared], "account-b", "account-a", auth, state, "node"
            )
            self.assertEqual([(item["key"], item["tokens"]) for item in after_switch], [("shared", 120)])

            shared.update(tokens=130, updated_at=210)
            other = {"key": "other", "created_at": 210, "updated_at": 210, "tokens": 40}
            while_other = track_target_usage(
                [old, shared, other], "account-b", "account-a", auth, state, "node"
            )
            self.assertEqual([(item["key"], item["tokens"]) for item in while_other], [("shared", 120)])

            os.utime(auth, (300, 300))
            shared.update(tokens=160, updated_at=310)
            other.update(tokens=60, updated_at=310)
            switched_back = track_target_usage(
                [old, shared, other], "account-a", "account-a", auth, state, "node"
            )
            self.assertEqual(
                [(item["key"], item["tokens"]) for item in switched_back],
                [("shared", 150), ("other", 20)],
            )

    def test_v043_account_binding_migrates_to_target_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth.json"
            state = root / "collector-state.json"
            auth.write_text("{}")
            state.write_text(json.dumps({
                "version": 1,
                "profiles": {"node": {"sessions": {"session": "account-a"}}},
            }))
            result = track_target_usage(
                [{"key": "session", "created_at": 1, "tokens": 75}],
                "account-a", "account-a", auth, state, "node",
            )
            self.assertEqual(result[0]["tokens"], 75)

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
            with closing(sqlite3.connect(state)) as db, db:
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
