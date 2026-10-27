import base64
import hashlib
import hmac
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from server import Collector, Config, UsageStore, build_handler


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

        self.store.read_account = Mock(return_value={"key": "aaaaaaaaaaaa", "name": "Test", "email": None, "plan": None})
        self.store.save_account("aaaaaaaaaaaa", make_default=True)

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

    def report(self, node_key="node-a", tokens=100, lifetime=1000, observed_at=None):
        usage = {
            "source": "codex-app-server", "summary": {"lifetime_tokens": lifetime},
            "daily_usage": [{"date": "2026-10-05", "tokens": 100}],
            "rate_limits": [{"id": "codex", "primary": {
                "used_percent": 20, "window_minutes": 10080, "resets_at": 1_500_000,
            }}],
        }
        if observed_at is not None:
            usage["observed_at"] = observed_at
        return {
            "node": {"key": node_key, "hostname": "worker", "os_user": node_key},
            "account": {"key": "aaaaaaaaaaaa"},
            "sessions": [{"key": "shared-session", "project_key": "project",
                          "tokens": tokens, "created_at": 800_000, "updated_at": 1_000_030}],
            "account_usage": usage,
        }

    def test_stale_account_report_does_not_change_weekly_history(self):
        self.store.ingest(self.report(lifetime=1000), now=1_000_000)
        stale = self.report(lifetime=900, observed_at=999999)
        stale["account_usage"]["rate_limits"][0]["primary"]["used_percent"] = 5
        self.store.ingest(stale, now=1_000_030)
        weekly = self.store.dashboard(now=1_000_030)["weekly_usage"]
        self.assertEqual(weekly["used_percent"], 20)
        self.assertEqual(weekly["points"][-1]["used_percent"], 20)

    def test_fresh_counter_correction_keeps_live_quota_updates_and_current_usage(self):
        self.store.ingest(self.report(lifetime=1000, observed_at=1000000), now=1000000)
        fresh = self.report(lifetime=900, observed_at=1000030)
        fresh["account_usage"]["rate_limits"][0]["primary"]["used_percent"] = 21
        self.store.ingest(fresh, now=1000030)
        data = self.store.dashboard(now=1000030)
        self.assertEqual(data["weekly_usage"]["used_percent"], 21)
        self.assertEqual(data["weekly_usage"]["points"][-1]["used_percent"], 21)
        self.assertEqual(data["account_usage"]["summary"]["lifetime_tokens"], 900)
        self.assertEqual(data["account_usage"]["counter_correction"]["previous_tokens"], 1000)
        self.assertEqual(data["account_usage"]["received_at"], "1970-01-12T13:47:10Z")

    def test_delayed_report_with_equal_lifetime_cannot_revert_weekly_reset(self):
        latest = self.report(observed_at=1_100_010)
        latest["account_usage"]["rate_limits"][0]["primary"].update(
            used_percent=0, resets_at=1_704_800,
        )
        self.store.ingest(latest, now=1_100_010)
        self.store.ingest(self.report(observed_at=1_000_000), now=1_100_040)
        weekly = self.store.dashboard(now=1_100_040)["weekly_usage"]
        self.assertEqual(weekly["resets_at"], "1970-01-20T17:33:20Z")
        self.assertEqual(weekly["used_percent"], 0)

    def test_partial_account_queries_preserve_last_successful_fields(self):
        self.store.ingest(self.report(), now=1_000_000)
        limits_only = self.report(lifetime=None)
        limits_only["account_usage"]["daily_usage"] = []
        limits_only["account_usage"]["rate_limits"][0]["primary"]["used_percent"] = 25
        self.store.ingest(limits_only, now=1_000_030)
        usage = self.store.read_account_usage()
        self.assertEqual(usage["summary"]["lifetime_tokens"], 1000)
        self.assertEqual(usage["daily_usage"], [{"date": "2026-10-05", "tokens": 100}])
        self.assertEqual(usage["rate_limits"][0]["primary"]["used_percent"], 25)
        usage_only = self.report(lifetime=1100)
        usage_only["account_usage"]["rate_limits"] = []
        self.store.ingest(usage_only, now=1_000_060)
        usage = self.store.read_account_usage()
        self.assertEqual(usage["summary"]["lifetime_tokens"], 1100)
        self.assertEqual(usage["rate_limits"][0]["primary"]["used_percent"], 25)

    def test_collect_uses_the_same_local_collector_sessions_as_dashboard(self):
        node_key = self.store.local_node_key()
        self.store.ingest(self.report(node_key=node_key), now=1_000_000)
        self.store.collect(now=1_000_000)
        self.store.ingest(self.report(node_key=node_key, tokens=150, lifetime=1100), now=1_000_030)
        self.store.collect(now=1_000_030)
        dashboard = self.store.dashboard(now=1_000_031)
        self.assertEqual(dashboard["sessions"][0]["delta"], 50)
        self.assertEqual(dashboard["history"][-1]["sessions"], 1)
        self.assertEqual(dashboard["reconciliation"]["period"]["attributed_session_tokens"], 50)

    def test_session_baselines_are_separate_for_each_node(self):
        with patch.object(self.store, "read_sessions", return_value=[]):
            self.store.ingest(self.report("node-a", tokens=100), now=1_000_000)
            self.store.ingest(self.report("node-b", tokens=500), now=1_000_000)
            self.store.collect(now=1_000_000)
            self.store.ingest(self.report("node-a", tokens=150, lifetime=1300), now=1_000_030)
            self.store.ingest(self.report("node-b", tokens=700, lifetime=1300), now=1_000_030)
            self.store.collect(now=1_000_030)
            dashboard = self.store.dashboard(now=1_000_031)
        deltas = {session["node_key"]: session["delta"] for session in dashboard["sessions"]}
        self.assertEqual(deltas, {"node-a": 50, "node-b": 200})
        self.assertEqual(dashboard["reconciliation"]["period"]["attributed_session_tokens"], 250)

    def test_remote_collecting_works_without_a_local_codex_database(self):
        self.source.unlink()
        self.store.ingest(self.report(), now=1_000_000)
        self.store.collect(now=1_000_000)
        self.assertEqual(self.store.dashboard(now=1_000_000)["summary"]["session_count"], 1)

    def test_ingest_rejects_invalid_payload_shapes_and_overlong_node_keys(self):
        for payload in ([], {"node": []}, {"node": self.report()["node"], "account": []},
                        self.report(node_key="x" * 65)):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.store.ingest(payload, now=1_000_000)

    def test_account_baseline_preserves_growth_within_one_sampling_bucket(self):
        with patch.object(self.store, "read_sessions", return_value=[]):
            self.store.ingest(self.report(lifetime=1000), now=1_000_000)
            self.store.collect(now=1_000_000)
            latest = self.report(tokens=120, lifetime=1050)
            latest["sessions"][0]["updated_at"] = 1_000_010
            self.store.ingest(latest, now=1_000_010)
            self.store.collect(now=1_000_010)
            period = self.store.dashboard(now=1_000_010)["reconciliation"]["period"]
        self.assertEqual(period["account_tokens"], 50)
        self.assertEqual(period["attributed_session_tokens"], 20)

    def test_period_combines_verified_and_snapshot_tokens_per_session(self):
        with patch.object(self.store, "read_sessions", return_value=[]):
            first = self.report()
            second_session = dict(first["sessions"][0], key="second-session", tokens=100)
            first["sessions"].append(second_session)
            self.store.ingest(first, now=1_000_000)
            self.store.collect(now=1_000_000)
            latest = self.report(tokens=200, lifetime=1500)
            latest["sessions"].append(dict(second_session, usage_buckets=[{"at": 1_000_020, "tokens": 150}]))
            self.store.ingest(latest, now=1_000_030)
            self.store.collect(now=1_000_030)
            # The minute starting at 1_000_020 is complete only after 1_000_080.
            period = self.store.dashboard(now=1_000_090)["reconciliation"]["period"]
        self.assertEqual(period["attributed_session_tokens"], 250)

    def test_existing_sessions_at_first_account_sample_do_not_count_as_growth(self):
        with patch.object(self.store, "read_sessions", return_value=[]):
            first = self.report()
            first["sessions"][0]["created_at"] = 1_000_000
            self.store.ingest(first, now=1_000_000)
            self.store.collect(now=1_000_000)
            latest = self.report(tokens=120, lifetime=1050)
            latest["sessions"][0]["created_at"] = 1_000_000
            self.store.ingest(latest, now=1_000_030)
            self.store.collect(now=1_000_030)
            period = self.store.dashboard(now=1_000_030)["reconciliation"]["period"]
        self.assertEqual(period["attributed_session_tokens"], 20)

    def test_legacy_snapshot_migration_keeps_history_and_assigns_known_nodes(self):
        self.store.ingest(self.report(), now=1_000_000)
        with closing(sqlite3.connect(self.monitor)) as db, db:
            db.execute("DROP TABLE session_samples")
            db.execute("""CREATE TABLE session_samples (
                observed_at INTEGER NOT NULL, session_key TEXT NOT NULL,
                project_key TEXT NOT NULL, model TEXT NOT NULL, tokens_used INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, archived INTEGER NOT NULL,
                PRIMARY KEY (observed_at, session_key))""")
            db.executemany("INSERT INTO session_samples VALUES (?, ?, ?, ?, ?, ?, ?)", [
                (1_000_000, "shared-session", "project", "gpt-test", 100, 1_000_000, 0),
                (1_000_000, "local-session", "project", "gpt-test", 75, 1_000_000, 0),
            ])
        self.store = UsageStore(self.store.config)
        with closing(sqlite3.connect(self.monitor)) as db:
            rows = db.execute("SELECT node_key, session_key, tokens_used FROM session_samples ORDER BY session_key").fetchall()
        self.assertEqual(rows, [(self.store.local_node_key(), "local-session", 75),
                                ("node-a", "shared-session", 100)])
        # A second initialization must be safe and keep the migrated rows.
        UsageStore(self.store.config)
        with patch.object(self.store, "read_sessions", return_value=[]):
            self.store.ingest(self.report(tokens=150, lifetime=1100), now=1_000_030)
            self.store.collect(now=1_000_030)
            dashboard = self.store.dashboard(now=1_000_031)
        self.assertEqual(dashboard["sessions"][0]["delta"], 50)
        self.assertEqual(dashboard["reconciliation"]["period"]["attributed_session_tokens"], 50)

    def handler(self, payload, path="/api/ingest"):
        body = json.dumps(payload).encode()
        handler = object.__new__(build_handler(self.store, Collector(self.store)))
        handler.path = path
        handler.rfile = io.BytesIO(body)
        handler.headers = {
            "Content-Length": str(len(body)), "X-Codex-Timestamp": "1000000",
            "X-Codex-Signature": hmac.new(self.store.collector_token.encode(), b"1000000." + body, hashlib.sha256).hexdigest(),
            "Authorization": "Basic " + base64.b64encode((self.store.dashboard_user + ":" + self.store.dashboard_password).encode()).decode(),
        }
        handler.send_json = Mock()
        return handler

    def test_invalid_http_payloads_return_400(self):
        for payload in (None, [], {"node": [1]}):
            handler = self.handler(payload)
            with patch("server.time.time", return_value=1_000_000):
                handler.do_POST()
            self.assertEqual(handler.send_json.call_args.args[1], 400)
        handler = self.handler([], f"/api/nodes/{self.store.local_node_key()}/alias")
        handler.do_PUT()
        self.assertEqual(handler.send_json.call_args.args[1], 400)

    def test_http_collect_failure_returns_retryable_error(self):
        handler = self.handler(self.report())
        with patch("server.time.time", return_value=1_000_000), patch.object(self.store, "collect", side_effect=sqlite3.OperationalError("locked")):
            handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[1], 503)

    def test_http_dashboard_login_supports_unicode_passwords(self):
        self.store.dashboard_password = "테스트-비밀번호"
        self.assertTrue(self.handler({}).dashboard_authorized())

    def test_collector_can_discover_an_account_without_registering_it(self):
        key = "cccccccccccc"
        handler = self.handler({"account": {"key": key, "email": "candidate@example.com"}}, "/api/collector/config")
        with patch("server.time.time", return_value=1000000):
            handler.do_POST()
        response = handler.send_json.call_args.args[0]
        self.assertNotIn(key, response["monitored_accounts"])
        self.assertEqual(handler.send_json.call_args.kwargs["response_token"], self.store.collector_token)
        self.assertTrue(any(row["account_key"] == key for row in self.store.accounts()["discovered"]))

    def test_account_registration_requires_dashboard_login_and_same_origin_json(self):
        handler = self.handler({"account_key": "cccccccccccc"}, "/api/accounts")
        handler.request_dashboard_login = Mock()
        handler.headers["Authorization"] = "invalid"
        handler.do_POST()
        handler.request_dashboard_login.assert_called_once()
        handler = self.handler({"account_key": "cccccccccccc"}, "/api/accounts")
        handler.headers.update({"Content-Type": "text/plain", "Origin": "https://foreign.example", "Host": "monitor.example"})
        handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[1], 403)
        handler.headers.update({"Content-Type": "application/json", "Origin": "https://monitor.example"})
        handler.rfile.seek(0)
        handler.do_POST()
        self.assertTrue(handler.send_json.call_args.args[0]["ok"])

    def test_ingest_adds_remote_node_without_raw_content(self):
        self.store.save_account("bbbbbbbbbbbb")
        payload = {
            "node": {"key": "remote-node", "hostname": "worker-1", "os_user": "alice"},
            "account": {"key": "bbbbbbbbbbbb", "name": "Alice", "email": "alice@example.com", "plan": "plus"},
            "sessions": [{
                "key": "hashed-session", "project_key": "hashed-project", "project_name": "frontend",
                "model": "gpt-test", "tokens": 900, "created_at": 900, "updated_at": 995, "archived": False,
            }],
        }
        self.store.ingest(payload, now=1000)
        dashboard = self.store.dashboard(now=1000, account_key="bbbbbbbbbbbb")
        remote = next(session for session in dashboard["sessions"] if session["source"] == "collector")
        self.assertEqual(remote["project_name"], "frontend")
        self.assertEqual(remote["hostname"], "worker-1")
        self.assertTrue(any(node["account"].get("email") == "alice@example.com" for node in dashboard["nodes"]))

    def test_daily_user_trend_separates_weekdays_weekends_and_nodes(self):
        def at(day, hour=12):
            return int(datetime(2026, 9, day, hour).timestamp())

        now = at(14, 20)  # Monday, server-local calendar date.
        def report(node_key, os_user, buckets):
            return {
                "node": {"key": node_key, "hostname": "worker", "os_user": os_user},
                "account": {"key": "aaaaaaaaaaaa"},
                "sessions": [{
                    "key": f"session-{node_key}", "project_key": "project", "model": "gpt-test",
                    "tokens": 1000, "created_at": at(12), "updated_at": now,
                    "usage_buckets": [{"at": at(day), "tokens": tokens} for day, tokens in buckets],
                }],
            }

        alice = report("node-a", "alice", [(12, 100), (13, 150), (14, 200)])
        bob = report("node-b", "bob", [(12, 50), (14, 300)])
        self.store.ingest(alice, now=now)
        self.store.ingest(alice, now=now + 1)  # Repeated reports must not double-count.
        self.store.ingest(bob, now=now)
        self.store.set_node_alias("node-a", "Alice Team")

        dashboard = self.store.dashboard(now=now)
        trend = dashboard["daily_user_trend"]
        days = {day["date"]: day for day in trend["days"]}
        self.assertEqual(len(trend["days"]), 14)
        self.assertEqual(days["2026-09-12"], {
            "date": "2026-09-12", "day_type": "weekend", "total_tokens": 150,
            "users": {"node-a": 100, "node-b": 50},
        })
        self.assertEqual(days["2026-09-13"]["total_tokens"], 150)
        self.assertEqual(days["2026-09-14"]["day_type"], "weekday")
        self.assertEqual(days["2026-09-14"]["users"], {"node-a": 200, "node-b": 300})
        self.assertEqual(days["2026-09-11"]["total_tokens"], 0)
        self.assertEqual(next(node for node in dashboard["nodes"] if node["key"] == "node-a")["display_name"], "Alice Team")
        self.assertNotIn("session-node-a", str(trend))

    def test_account_usage_is_canonical_and_not_summed_across_nodes(self):
        def report(node_key, hostname, lifetime, used_percent):
            return {
                "node": {"key": node_key, "hostname": hostname, "os_user": "alice"},
                "account": {"key": "aaaaaaaaaaaa", "email": "shared@example.com"},
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
                "account": {"key": "aaaaaaaaaaaa"},
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
        self.assertEqual(worker["status"], "throughput")
        self.assertEqual(worker["basis"], "token_throughput")

    def test_weekly_breakdown_counts_users_and_untracked_tokens_from_same_period(self):
        def report(node_key, tokens, lifetime):
            return {
                "node": {"key": node_key, "hostname": node_key, "os_user": node_key},
                "account": {"key": "aaaaaaaaaaaa"},
                "sessions": [{
                    "key": f"session-{node_key}", "project_key": "project", "model": "gpt-test",
                    "tokens": tokens, "created_at": 900_000, "updated_at": 1_000_030,
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
            self.store.ingest(report("node-a", 100, 1000), now=1_000_000)
            self.store.ingest(report("node-b", 50, 1000), now=1_000_001)
            self.store.collect(now=1_000_001)
            self.store.ingest(report("node-a", 200, 1300), now=1_000_030)
            self.store.ingest(report("node-b", 100, 1300), now=1_000_031)
            self.store.collect(now=1_000_031)
            period = self.store.dashboard(now=1_000_031)["reconciliation"]["period"]

        self.assertEqual(period["account_tokens"], 300)
        self.assertEqual(period["account_sample_count"], 4)
        self.assertEqual(period["attributed_session_tokens"], 150)
        self.assertEqual(period["difference_tokens"], 150)
        self.assertEqual(period["untracked_tokens"], 150)
        self.assertEqual(period["over_attributed_tokens"], 0)
        self.assertEqual(period["participant_count"], 2)
        self.assertEqual(period["user_tokens"], [
            {"node_key": "node-a", "tokens": 100},
            {"node_key": "node-b", "tokens": 50},
        ])

        with patch.object(self.store, "read_sessions", return_value=[]):
            self.store.ingest(report("node-a", 600, 1300), now=1_000_060)
            self.store.collect(now=1_000_060)
            excess = self.store.dashboard(now=1_000_060)["reconciliation"]["period"]
        self.assertEqual(excess["difference_tokens"], -250)
        self.assertEqual(excess["untracked_tokens"], 0)
        self.assertEqual(excess["over_attributed_tokens"], 250)

    def test_weekly_breakdown_uses_verified_events_when_snapshot_delta_is_lower(self):
        def report(tokens, lifetime, buckets):
            return {
                "node": {"key": "node-a", "hostname": "worker", "os_user": "alice"},
                "account": {"key": "aaaaaaaaaaaa"},
                "sessions": [{
                    "key": "session-a", "project_key": "project", "model": "gpt-test",
                    "tokens": tokens, "created_at": 900_000, "updated_at": 1_000_030,
                    "usage_buckets": buckets,
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
            self.store.ingest(report(100, 1000, []), now=1_000_000)
            self.store.collect(now=1_000_000)
            self.store.ingest(report(150, 1300, [{"at": 1_000_020, "tokens": 120}]), now=1_000_030)
            self.store.collect(now=1_000_030)
            period = self.store.dashboard(now=1_000_090)["reconciliation"]["period"]

        self.assertEqual(period["account_tokens"], 300)
        self.assertEqual(period["attributed_session_tokens"], 120)
        self.assertEqual(period["user_tokens"], [{"node_key": "node-a", "tokens": 120}])
        self.assertEqual(period["untracked_tokens"], 180)

    def test_weekly_daily_history_survives_late_restart_and_constant_counter(self):
        start = int(datetime(2026, 10, 3, 6, 37, 17, tzinfo=timezone.utc).timestamp())
        now = int(datetime(2026, 10, 5, 8, tzinfo=timezone.utc).timestamp())
        usage = {
            "source": "codex-app-server", "summary": {"lifetime_tokens": 6000},
            "daily_usage": [
                {"date": "2026-10-02", "tokens": 1000},
                {"date": "2026-10-03", "tokens": 800},  # Cannot split the reset day.
                {"date": "2026-10-04", "tokens": 1000},
                {"date": "2026-10-05", "tokens": 999},  # Partial today, not a full day.
                {"date": "2026-10-06", "tokens": 999},
            ],
            "rate_limits": [{"id": "codex", "primary": {
                "window_minutes": 10080, "resets_at": start + 604800, "used_percent": 37,
            }}],
        }
        def report(key, buckets):
            return {
                "node": {"key": key, "hostname": key, "os_user": key},
                "account": {"key": "aaaaaaaaaaaa"}, "account_usage": usage,
                "sessions": [{"key": key, "tokens": 1_000_000, "created_at": start - 86400,
                              "updated_at": now, "usage_buckets": buckets}],
            }
        self.store.ingest(report("node-a", [
            {"at": start - 17, "tokens": 5000},  # Minute overlaps the reset; excluded.
            {"at": start + 43, "tokens": 100},
            {"at": now - 120, "tokens": 100},
        ]), now=now - 60)
        self.store.ingest(report("node-b", [{"at": now - 86400, "tokens": 50}]), now=now)
        dashboard = self.store.dashboard(now=now)
        weekly = dashboard["weekly_token_breakdown"]
        self.assertEqual(dashboard["reconciliation"]["period"]["account_tokens"], 0)
        self.assertEqual(weekly["account_tokens"], 1000)
        self.assertEqual(weekly["account_dates"], ["2026-10-04"])
        self.assertEqual(weekly["attributed_session_tokens"], 250)
        self.assertEqual(weekly["untracked_tokens"], 750)
        self.assertEqual(weekly["participant_count"], 2)
        self.assertTrue(weekly["is_lower_bound"])
        self.assertTrue(weekly["comparable"])

        # Missing/lagging account data cannot turn an oversized user sum into
        # 100% attribution with zero untracked tokens.
        usage["daily_usage"][2]["tokens"] = 100
        self.store.ingest(report("node-b", [{"at": now - 86400, "tokens": 50}]), now=now + 30)
        insufficient = self.store.dashboard(now=now + 30)["weekly_token_breakdown"]
        self.assertFalse(insufficient["comparable"])
        self.assertIsNone(insufficient["untracked_tokens"])

    def test_weekly_breakdown_does_not_invent_missing_account_total(self):
        weekly = {"window_started_at": "2026-10-03T06:37:17Z", "resets_at": "2026-10-10T06:37:17Z"}
        result = self.store.read_weekly_token_breakdown(
            {"daily_usage": []}, weekly, [], None,
            int(datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()),
        )
        self.assertIsNone(result["account_tokens"])
        self.assertIsNone(result["untracked_tokens"])
        self.assertFalse(result["comparable"])

    def test_monthly_analysis_retains_reported_events_beyond_two_weeks(self):
        now = int(datetime(2026, 10, 6, tzinfo=timezone.utc).timestamp())
        report = self.report()
        report["sessions"][0]["usage_buckets"] = [
            {"at": now - 20 * 86400, "tokens": 100},
            {"at": now - 94 * 86400, "tokens": 999},
        ]
        self.store.ingest(report, now=now)
        with self.store._connect_monitor() as db:
            rows = db.execute("SELECT tokens_used FROM collector_session_usage").fetchall()
        self.assertEqual([row["tokens_used"] for row in rows], [100])

    def test_new_user_without_events_appears_in_all_weekly_views(self):
        start = int(datetime(2026, 10, 3, 6, tzinfo=timezone.utc).timestamp())
        now = start + 3 * 86400
        report = self.report(node_key="new-user")
        report["account_usage"]["rate_limits"][0]["primary"]["resets_at"] = start + 604800
        report["account_usage"]["daily_usage"] = [{"date": "2026-10-04", "tokens": 1000}]
        report["sessions"] = [
            {"key": "new", "tokens": 200, "created_at": now - 3600, "updated_at": now - 600},
            {"key": "archived", "tokens": 50, "created_at": now - 4000, "updated_at": now - 900, "archived": True},
            {"key": "old", "tokens": 1_000_000, "created_at": start - 86400, "updated_at": now - 900},
        ]
        with patch.object(self.store, "read_sessions", return_value=[]):
            self.store.ingest(report, now=now)
            self.store.collect(now=now)
            dashboard = self.store.dashboard(now=now)
        breakdown = dashboard["weekly_token_breakdown"]
        self.assertEqual(breakdown["user_tokens"], [{"node_key": "new-user", "tokens": 250}])
        self.assertEqual(breakdown["user_tokens"], dashboard["weekly_user_tokens"])
        self.assertEqual(breakdown["snapshot_only_tokens"], 250)
        self.assertEqual(breakdown["verified_event_tokens"], 0)
        self.assertEqual(breakdown["untracked_tokens"], 750)
        comparison = next(item for item in dashboard["reconciliation"]["node_comparison"] if item["node_key"] == "new-user")
        self.assertEqual(comparison["tokens"], 250)
        visible = {session["key"]: session for session in dashboard["sessions"]}
        self.assertEqual(visible["new"]["weekly_tokens"], 200)
        self.assertEqual(visible["new"]["verified_weekly_tokens"], 0)
        self.assertEqual(visible["old"]["weekly_tokens"], 0)
        self.assertNotIn("archived", visible)
        # No usage is silently assigned to a calendar day without timestamps.
        self.assertEqual(sum(day["total_tokens"] for day in dashboard["daily_user_trend"]["days"]), 0)

    def test_event_boundaries_are_shared_by_weekly_daily_and_session_totals(self):
        start = int(datetime(2026, 10, 3, 6, 0, 17, tzinfo=timezone.utc).timestamp())
        now = start + 3 * 86400
        report = self.report()
        report["account_usage"]["rate_limits"][0]["primary"]["resets_at"] = start + 604800
        report["account_usage"]["daily_usage"] = [{"date": "2026-10-04", "tokens": 1000}]
        report["sessions"] = [{
            "key": "s", "tokens": 0, "created_at": start - 86400, "updated_at": now,
            "usage_buckets": [
                {"at": start - 17, "tokens": 900},
                {"at": start + 43, "tokens": 100},
                {"at": now - 17, "tokens": 500},
            ],
        }]
        with patch.object(self.store, "read_sessions", return_value=[]):
            self.store.ingest(report, now=now)
            self.store.ingest(report, now=now)  # Repeated report must not double count.
            dashboard = self.store.dashboard(now=now)
        self.assertEqual(dashboard["weekly_token_breakdown"]["attributed_session_tokens"], 100)
        self.assertEqual(dashboard["sessions"][0]["verified_weekly_tokens"], 100)
        self.assertEqual(dashboard["reconciliation"]["node_comparison"][0]["tokens"], 100)
        # The pre-reset minute belongs to the daily graph, but the unfinished
        # current minute is excluded from every view.
        self.assertEqual(sum(day["total_tokens"] for day in dashboard["daily_user_trend"]["days"]), 1000)

    def test_pre_reset_snapshot_is_not_used_as_weekly_baseline(self):
        from usage_aggregation import UsageWindow
        report = self.report()
        report["sessions"][0].update(tokens=100, created_at=800, updated_at=990)
        with patch.object(self.store, "read_sessions", return_value=[]):
            self.store.ingest(report, now=990)
            self.store.collect(now=990)
            report["sessions"][0].update(tokens=200, updated_at=1010)
            self.store.ingest(report, now=1010)
            self.store.collect(now=1010)
            report["sessions"][0].update(tokens=250, updated_at=1020)
            self.store.ingest(report, now=1020)
            nodes, sessions = self.store.read_current_sessions(1020)
            attribution = self.store.read_period_attribution(sessions, nodes, UsageWindow(1000, 1020))
        self.assertEqual(sum(item.tokens for item in attribution.values()), 50)

    def test_node_comparison_shows_range_for_usage_before_first_observation(self):
        def report(tokens, lifetime):
            return {
                "node": {"key": "node-a", "hostname": "worker-a", "os_user": "alice"},
                "account": {"key": "aaaaaaaaaaaa"},
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
            "account": {"key": "aaaaaaaaaaaa"},
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
            baseline = self.report(tokens=0, lifetime=4000)
            baseline["sessions"] = []
            baseline["account_usage"]["rate_limits"][0]["primary"]["used_percent"] = 20
            self.store.ingest(baseline, now=999900)
            report["sessions"][0]["usage_buckets"] = [{"at": 999900, "tokens": 250}]
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
        self.assertEqual(response["alerts"][0]["node_used_percent"], 10)
        self.assertNotIn("alerts", repeated)

    def test_quota_comparison_and_alert_prefer_measured_consumption_over_token_volume(self):
        baseline = self.report(tokens=0, lifetime=4000)
        baseline["sessions"] = []
        cheap = self.report(node_key="cheap", tokens=1_000_000, lifetime=1_004_000)
        cheap["sessions"][0]["usage_buckets"] = [{"at": 999780, "tokens": 1_000_000}]
        cheap["account_usage"]["rate_limits"][0]["primary"]["used_percent"] = 21
        expensive = self.report(node_key="expensive", tokens=10, lifetime=1_004_010)
        expensive["sessions"][0]["usage_buckets"] = [{"at": 999900, "tokens": 10}]
        expensive["account_usage"]["rate_limits"][0]["primary"]["used_percent"] = 30
        with patch.object(self.store, "read_sessions", return_value=[]):
            self.store.ingest(baseline, now=999720)
            self.store.ingest(cheap, now=999840)
            self.store.ingest(expensive, now=999960)
            with closing(sqlite3.connect(self.monitor)) as db, db:
                # Previously queued token-based warnings must not leak through
                # after the quota policy changes, even for the same target.
                db.execute("INSERT INTO collector_alerts VALUES (?, ?, ?, ?, NULL)",
                           ("f" * 24, "expensive", json.dumps({"account_key": "aaaaaaaaaaaa"}), 999960))
            dashboard = self.store.dashboard(now=1000000)
            self.assertNotIn("alerts", self.store.collector_response("cheap", {}, now=1000000))
            alerts = self.store.collector_response("expensive", {}, now=1000000)["alerts"]
            self.assertEqual(len(alerts), 1)
            alert = alerts[0]
        quota = dashboard["weekly_quota"]
        self.assertEqual(quota["used_percent"], 30)
        self.assertEqual(quota["remaining_percent"], 70)
        self.assertEqual(quota["attributed_percent"], 10)
        self.assertEqual(quota["user_usage"][0]["node_key"], "expensive")
        self.assertEqual(quota["user_usage"][0]["used_percent"], 9)
        self.assertEqual(alert["node_used_percent"], 9)

    def test_initial_quota_without_observed_growth_does_not_target_a_token_heavy_user(self):
        report = self.report(tokens=1_000_000)
        report["account_usage"]["rate_limits"][0]["primary"]["used_percent"] = 80
        self.store.ingest(report, now=1000000)
        result = self.store.collector_response("node-a", {}, now=1000000)
        self.assertNotIn("alerts", result)

    def test_request_details_are_idempotent_and_do_not_invent_personal_quota(self):
        report = self.report(tokens=1210)
        report["collector"] = {"version": "0.7.1"}
        report["sessions"][0]["usage_detail_backfill_complete"] = True
        report["sessions"][0]["usage_details"] = [
            {"at": 999960, "model": "small-model", "effort": "low", "service_tier": "standard",
             "reasoning_mode": "standard", "tokens": 1010, "events": 1,
             "input_tokens": 1000, "cached_input_tokens": 990, "output_tokens": 10, "reasoning_output_tokens": 0},
            {"at": 999960, "model": "large-model", "effort": "high", "service_tier": "standard",
             "reasoning_mode": "standard", "tokens": 200, "events": 1,
             "input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 100, "reasoning_output_tokens": 80},
        ]
        self.store.ingest(report, now=1000030)
        self.store.ingest(report, now=1000030)
        user = next(row for row in self.store.dashboard(now=1000030)["weekly_quota"]["user_usage"] if row["node_key"] == "node-a")
        self.assertIsNone(user["used_percent"])
        self.assertNotIn("estimated_used_percent", user)
        self.assertEqual(sum(row["tokens"] for row in user["usage_details"]), 1210)
        self.assertEqual(user["usage_details"][0]["model"], "small-model")
        self.assertEqual(user["usage_details"][0]["cached_input_tokens"], 990)
        self.assertEqual(user["collector_metadata"]["version"], "0.7.1")

    def test_active_machine_collector_keeps_a_legacy_user_task_from_replacing_its_report(self):
        machine = self.report(tokens=100)
        machine["collector"] = {"version": "0.7.1", "kind": "windows-machine"}
        legacy = self.report(tokens=999999)
        legacy["collector"] = {"version": "0.7.4", "kind": "user"}
        self.store.ingest(machine, now=1000000)
        self.store.ingest(legacy, now=1000030)
        with closing(sqlite3.connect(self.monitor)) as db:
            self.assertEqual(db.execute("SELECT tokens_used FROM collector_sessions WHERE node_key='node-a'").fetchone()[0], 100)
            self.assertEqual(db.execute("SELECT kind FROM collector_usage_metadata WHERE node_key='node-a'").fetchone()[0], "windows-machine")
        # A stopped machine task must not permanently suppress the user task.
        self.store.ingest(legacy, now=1000200)
        with closing(sqlite3.connect(self.monitor)) as db:
            self.assertEqual(db.execute("SELECT tokens_used FROM collector_sessions WHERE node_key='node-a'").fetchone()[0], 999999)

    def test_machine_preference_does_not_block_another_account_on_the_same_node(self):
        self.store.save_account("bbbbbbbbbbbb")
        machine = self.report(tokens=100)
        machine["collector"] = {"version": "0.7.1", "kind": "windows-machine"}
        other = self.report(tokens=200)
        other["account"]["key"] = "bbbbbbbbbbbb"
        other["collector"] = {"version": "0.7.4", "kind": "user"}
        self.store.ingest(machine, now=1000000)
        self.store.ingest(other, now=1000030)
        with closing(sqlite3.connect(self.monitor)) as db:
            self.assertEqual(db.execute("SELECT tokens_used FROM collector_sessions WHERE account_key='bbbbbbbbbbbb'").fetchone()[0], 200)

    def test_weekly_usage_history_starts_over_without_resetting_total_processed(self):
        def report(used_percent, resets_at, lifetime=5000):
            return {
                "node": {"key": "node-a", "hostname": "worker-a", "os_user": "alice"},
                "account": {"key": "aaaaaaaaaaaa"}, "sessions": [],
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
            "account": {"key": "cccccccccccc"},
            "sessions": [{
                "key": "old-session", "project_key": "old-project", "project_name": "private",
                "model": "gpt-test", "tokens": 500, "created_at": 900, "updated_at": 995,
                "archived": False,
            }],
        }
        self.store.save_account("cccccccccccc")
        self.store.ingest(payload, now=1000)
        with patch.object(self.store, "collector_account_key", return_value="aaaaaaaaaaaa"):
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
