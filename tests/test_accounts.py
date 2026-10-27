import json
import os
import sqlite3
import tempfile
import subprocess
import sys
import tarfile
import unittest
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from collector import (attribution_scope, collector_configuration, configure_collector,
                       monitoring_configuration, registered_snapshots, track_target_usage)
from server import Config, UsageStore


A, B = "aaaaaaaaaaaa", "bbbbbbbbbbbb"


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth.json"
        self.auth.write_text("{}")
        self.state = self.root / "collector-state.json"
        self.config = Config(self.root / "missing-source", self.root / "monitor.sqlite", "salt",
                             auth_file=self.root / "missing-auth", collector_token_file=self.root / "token",
                             dashboard_password_file=self.root / "password")
        self.store = UsageStore(self.config)
        self.store.save_account(A, "Account A", make_default=True)
        self.store.save_account(B, "Account B")

    def tearDown(self):
        self.temp.cleanup()

    def report(self, key, tokens, lifetime, at):
        return {"node": {"key": "same-node", "hostname": "worker", "os_user": "user"},
                "account": {"key": key},
                "sessions": [{"key": "same-session", "project_key": "project", "tokens": tokens,
                              "created_at": 800000, "updated_at": at,
                              "usage_buckets": [{"at": 1000020, "tokens": tokens}]}],
                "account_usage": {"source": "codex-app-server", "summary": {"lifetime_tokens": lifetime},
                                  "rate_limits": [{"id": "codex", "primary": {
                                      "used_percent": 20, "window_minutes": 10080, "resets_at": 1500000}}]}}

    def test_same_node_and_session_are_separate_in_every_account_view(self):
        for key, tokens, lifetime in ((A, 100, 1000), (B, 700, 9000)):
            self.store.ingest(self.report(key, tokens, lifetime, 1000030), now=1000030)
        self.store.collect(now=1000030)
        self.store.collect(now=1000100)
        a = self.store.dashboard(now=1000100, account_key=A)
        b = self.store.dashboard(now=1000100, account_key=B)
        for data, tokens, lifetime in ((a, 100, 1000), (b, 700, 9000)):
            self.assertEqual(data["summary"]["total_tokens"], lifetime)
            self.assertEqual(data["summary"]["session_tokens"], tokens)
            self.assertEqual(data["history"][-1]["tokens"], lifetime)
            self.assertEqual(data["sessions"][0]["weekly_tokens"], tokens)
            self.assertEqual(sum(day["total_tokens"] for day in data["daily_user_trend"]["days"]), tokens)
            self.assertEqual(data["analytics"]["months"][0]["attributed_tokens"], tokens)
        self.store.ingest(self.report(B, 800, 9100, 1000130), now=1000130)
        self.assertEqual(self.store.dashboard(now=1000130, account_key=A)["summary"]["session_tokens"], 100)

    def test_login_changes_do_not_change_default_or_attribute_local_usage_to_it(self):
        with patch.object(self.store, "read_account", return_value={"key": B}):
            self.assertEqual(self.store.collector_account_key(), A)
            self.store.store_local_account_usage({"source": "codex-app-server", "summary": {"lifetime_tokens": 555}}, now=1000000)
            self.assertIsNone(self.store.read_account_usage())
            with self.store.account_scope(B):
                self.assertEqual(self.store.read_account_usage()["summary"]["lifetime_tokens"], 555)
        self.assertEqual(UsageStore(self.config).collector_account_key(), A)

    def test_account_percentages_stay_independent_of_token_volume_and_other_accounts(self):
        for key, tokens, percent in ((A, 1_000_000, 2), (B, 10, 80)):
            report = self.report(key, tokens, tokens, 1000030)
            report["account_usage"]["rate_limits"][0]["primary"]["used_percent"] = percent
            self.store.ingest(report, now=1000030)
        for key, expected in ((A, 2), (B, 80)):
            quota = self.store.dashboard(now=1000100, account_key=key)["weekly_quota"]
            self.assertEqual(quota["used_percent"], expected)
            self.assertEqual(quota["remaining_percent"], 100 - expected)
            self.assertEqual(quota["unattributed_percent"], expected)
            self.assertEqual(quota["attributed_percent"], 0)

    def test_raw_local_database_is_not_used_for_multiple_accounts(self):
        with patch.object(self.store, "read_account", return_value={"key": A}), patch.object(self.store, "read_sessions") as raw:
            self.assertEqual(self.store.read_current_sessions(1000000), ([], []))
        raw.assert_not_called()

    def test_discovery_requires_explicit_registration_and_disabled_history_remains(self):
        key = "cccccccccccc"
        result = self.store.discover_account({"account": {"key": key, "email": "candidate@example.com", "secret": "omit"}})
        self.assertNotIn(key, result["monitored_accounts"])
        self.assertNotIn("secret", str(self.store.accounts()))
        with self.assertRaises(ValueError):
            self.store.ingest(self.report(key, 50, 1000, 1000000), now=1000000)
        self.store.save_account(key)
        self.store.ingest(self.report(key, 50, 1000, 1000000), now=1000000)
        self.store.save_account(key, enabled=False)
        with self.assertRaises(ValueError):
            self.store.ingest(self.report(key, 60, 1100, 1000030), now=1000030)
        self.assertEqual(self.store.dashboard(now=1000030, account_key=key)["summary"]["session_tokens"], 50)

    def test_parallel_dashboards_do_not_share_account_context(self):
        for key, tokens in ((A, 100), (B, 700)):
            self.store.ingest(self.report(key, tokens, tokens * 10, 1000030), now=1000030)
        def read(key):
            return self.store.dashboard(now=1000100, account_key=key)["summary"]["session_tokens"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(executor.map(read, [A, B] * 3)), [100, 700] * 3)
        self.assertEqual(self.store.collector_account_key(), A)

    def test_multi_account_collector_preserves_legacy_history_and_counts_switches_once(self):
        token = "token"
        payload = self.report(A, 100, 1000, 1000)
        payload["sessions"][0].update(created_at=900, updated_at=1000)
        payload["sessions"][0].pop("usage_buckets")
        os.utime(self.auth, (800, 800))
        base = attribution_scope("same-node", token)
        with patch("collector.time.time", return_value=1000):
            track_target_usage(payload["sessions"], A, A, self.auth, self.state, base)
        targets = {"tracked_accounts": [A, B], "monitored_accounts": [A, B]}
        with patch("collector.time.time", return_value=1030):
            reports = registered_snapshots(payload, targets, self.auth, self.state, token, A)
        self.assertEqual(reports[0]["sessions"][0]["tokens"], 100)
        self.assertEqual(reports[1]["sessions"], [])
        payload["account"] = {"key": B}
        payload["sessions"][0].update(tokens=150, updated_at=1060)
        os.utime(self.auth, (1040, 1040))
        with patch("collector.time.time", return_value=1060):
            reports = registered_snapshots(payload, targets, self.auth, self.state, token, A)
        self.assertEqual(reports[0]["sessions"][0]["tokens"], 100)
        self.assertEqual(reports[1]["sessions"][0]["tokens"], 50)
        with patch("collector.time.time", return_value=1090):
            again = registered_snapshots(payload, targets, self.auth, self.state, token, A)
        self.assertEqual(again, reports)
        targets["monitored_accounts"] = [A]
        payload["sessions"][0].update(tokens=200, updated_at=1120)
        with patch("collector.time.time", return_value=1120):
            registered_snapshots(payload, targets, self.auth, self.state, token, A)
        targets["monitored_accounts"] = [A, B]
        payload["sessions"][0].update(tokens=230, updated_at=1150)
        with patch("collector.time.time", return_value=1150):
            resumed = registered_snapshots(payload, targets, self.auth, self.state, token, A)
        self.assertEqual(resumed[1]["sessions"][0]["tokens"], 80)

    def test_server_configuration_failure_cannot_fall_back_to_collecting_all_accounts(self):
        with patch("collector.send", return_value={"tracked_accounts": [A], "monitored_accounts": [B]}):
            with self.assertRaises(RuntimeError):
                monitoring_configuration("http://server", self.report(A, 1, 1, 1), "token", A)

    def test_legacy_migration_preserves_rows_and_is_idempotent(self):
        # Simulate the complete pre-account schema using the old column lists.
        self.store.ingest(self.report(A, 100, 1000, 1000030), now=1000030)
        self.store.collect(now=1000030)
        with closing(sqlite3.connect(self.config.monitor_db)) as db, db:
            for table in ("overview_samples", "session_samples", "collector_sessions", "collector_session_usage"):
                db.execute(f"DELETE FROM {table} WHERE account_key=?", (B,))
                columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})") if row[1] != "account_key"]
                names = ",".join(columns)
                db.execute(f"CREATE TABLE {table}_legacy AS SELECT {names} FROM {table}")
                db.execute(f"DROP TABLE {table}")
                db.execute(f"ALTER TABLE {table}_legacy RENAME TO {table}")
            before = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in
                      ("overview_samples", "session_samples", "collector_sessions", "collector_session_usage")}
        migrated = UsageStore(self.config)
        UsageStore(self.config)
        with closing(sqlite3.connect(self.config.monitor_db)) as db:
            after = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
        self.assertEqual(before, after)
        self.assertEqual(migrated.collector_account_key(), A)


class ConfigurationTest(unittest.TestCase):
    def test_linux_bundle_can_be_reconfigured_after_install_without_reinstall(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "dummy.token"
            token.write_text("dummy-authentication-key")
            subprocess.run([sys.executable, "tools/build_collector_bundle.py", "--without-token", "--account-key", A,
                            "--server", "http://initial-server:8765", "--output-dir", str(root)], cwd=repository, check=True, capture_output=True)
            archive = next(root.glob("*.tar.gz"))
            with tarfile.open(archive) as package:
                package.extractall(root, filter="data")
            bundle = next(path for path in root.glob("codex-usage-collector-*") if path.is_dir())
            installed = root / "installed"
            environment = dict(os.environ, CODEX_COLLECTOR_INSTALL_ROOT=str(installed))
            subprocess.run(["bash", str(bundle / "install.sh"), "--token-file", str(token), "--no-start"], env=environment, check=True, capture_output=True)
            configure = installed / ".local/share/codex-usage-collector/configure.sh"
            subprocess.run(["bash", str(configure), "--server", "https://new-server:9443"],
                           env=dict(os.environ, CODEX_COLLECTOR_CONFIG_FILE=str(installed / ".config/collector-config.json")), check=True, capture_output=True)
            settings = json.loads((installed / ".config/collector-config.json").read_text())
            self.assertEqual(settings["server"], "https://new-server:9443")
            self.assertEqual(Path(settings["token_file"]).read_text().strip(), "dummy-authentication-key")
            # A new package default must not relabel the installed account A
            # ledger, nor reset the destination chosen after installation.
            (bundle / "bundle.conf").write_text(f"server=http://bundle-server\naccount_key={B}\n")
            subprocess.run(["bash", str(bundle / "install.sh"), "--no-start"], env=environment, check=True, capture_output=True)
            after = json.loads((installed / ".config/collector-config.json").read_text())
            self.assertEqual(after["server"], "https://new-server:9443")
            self.assertEqual(after["account_key"], A)

    def test_saved_server_overrides_old_task_arguments_and_keeps_token_on_url_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "token"
            token.write_text("private-token")
            config = root / "config.json"
            config.write_text(json.dumps({"server": "http://old-server:8765", "token_file": str(token), "account_key": A}))
            args = Namespace(config_file=config, token_file=root / "nonexistent-default", server="http://new-server:9876/",
                             account_key=None, configure=True, show_config=False)
            with patch("collector.sys.argv", ["collector.py", "--configure", "--server", args.server]):
                configure_collector(args)
            args.server = "http://old-task-server"
            _, settings = collector_configuration(args)
            self.assertEqual(settings["server"], "http://new-server:9876")
            self.assertEqual(settings["token_file"], str(token))
            self.assertEqual(settings["account_key"], A)
            self.assertNotIn("private-token", config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            previous = config.read_bytes()
            args.server = "http://host\nCODEX_EVIL=true"
            with patch("collector.sys.argv", ["collector.py", "--server", args.server]), self.assertRaises(ValueError):
                configure_collector(args)
            self.assertEqual(config.read_bytes(), previous)

    def test_windows_utf8_configuration_and_live_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "settings.json"
            args = Namespace(config_file=config, token_file=root / "token", server=None, account_key=A)
            config.write_text(json.dumps({"server": "https://first-server"}), encoding="utf-8-sig")
            self.assertEqual(collector_configuration(args)[1]["server"], "https://first-server")
            config.write_text(json.dumps({"server": "https://second-server:9443"}))
            self.assertEqual(collector_configuration(args)[1]["server"], "https://second-server:9443")
