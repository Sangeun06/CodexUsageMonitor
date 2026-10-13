import sqlite3
import unittest
from datetime import date, datetime, timezone

from usage_analytics import calendar_report, next_month, observed_account_days, read_analytics, timestamp
from usage_aggregation import UsageWindow


class CalendarAnalyticsTest(unittest.TestCase):
    def setUp(self):
        self.start = date(2026, 10, 1)
        self.end = date(2026, 11, 1)
        self.now = int(datetime(2026, 10, 6, 12, tzinfo=timezone.utc).timestamp())

    def report(self, accounts=None, events=None, sessions=None, window=None):
        return calendar_report(self.start, self.end, self.now, accounts or {}, events or [], sessions or [], {"a"}, window)

    def test_month_boundaries_including_leap_year(self):
        self.assertEqual(next_month(date(2024, 2, 1)), date(2024, 3, 1))
        self.assertEqual(next_month(date(2026, 12, 1)), date(2027, 1, 1))

    def test_missing_dates_are_not_zero_and_future_reports_are_excluded(self):
        report = self.report({"2026-10-01": 0, "2026-10-02": 100, "2026-10-08": 999})
        self.assertEqual(report["account_tokens"], 100)
        self.assertEqual(report["reported_days"], 2)
        self.assertEqual(report["elapsed_days"], 6)
        self.assertEqual(report["days"][0]["account_tokens"], 0)
        self.assertIsNone(report["days"][2]["account_tokens"])
        self.assertEqual(report["days"][7]["status"], "future")
        self.assertIsNone(report["days"][7]["account_tokens"])

    def test_same_day_new_session_is_visible_without_events_and_not_double_counted(self):
        at = timestamp(date(2026, 10, 5))
        session = {"node_key": "a", "key": "new", "tokens": 200, "created_at": at + 1, "updated_at": at + 600, "archived": True}
        event = {"node_key": "a", "session_key": "new", "date": "2026-10-05", "tokens": 100}
        report = self.report({"2026-10-05": 500}, [event], [session])
        day = report["days"][4]
        self.assertEqual(day["users"], {"a": 200})
        self.assertEqual(day["untracked_tokens"], 300)
        self.assertEqual(day["snapshot_only_tokens"], 100)

    def test_multi_day_counter_is_not_assigned_to_last_activity_date(self):
        session = {"node_key": "a", "key": "old", "tokens": 99999, "created_at": timestamp(self.start), "updated_at": self.now}
        self.assertEqual(self.report(sessions=[session])["attributed_tokens"], 0)

    def test_account_smaller_than_users_has_no_fabricated_untracked_amount(self):
        event = {"node_key": "a", "session_key": "s", "date": "2026-10-02", "tokens": 200}
        day = self.report({"2026-10-02": 100}, [event])["days"][1]
        self.assertFalse(day["comparable"])
        self.assertIsNone(day["untracked_tokens"])

    def test_hidden_nodes_and_future_sessions_are_excluded(self):
        event = {"node_key": "hidden", "session_key": "s", "date": "2026-10-02", "tokens": 999}
        session = {"node_key": "a", "key": "s", "tokens": 999, "created_at": self.now + 5, "updated_at": self.now + 10}
        self.assertEqual(self.report(events=[event], sessions=[session])["attributed_tokens"], 0)

    def test_weekly_boundary_day_never_uses_entire_account_day(self):
        window = UsageWindow(timestamp(self.start) + 3600, self.now)
        report = self.report({"2026-10-01": 999, "2026-10-02": 100}, window=window)
        self.assertIsNone(report["days"][0]["account_tokens"])
        self.assertEqual(report["account_tokens"], 100)

    def test_utc_dates_and_completed_minutes_in_repository(self):
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            db.execute("CREATE TABLE collector_session_usage (node_key TEXT, session_key TEXT, bucket_at INTEGER, tokens_used INTEGER)")
            boundary = timestamp(date(2026, 10, 6))
            db.executemany("INSERT INTO collector_session_usage VALUES ('a','s',?,?)", [(boundary - 60, 100), (boundary, 200), (self.now - 30, 999)])
            data = read_analytics(db, {}, None, [], [{"key": "a"}], self.now)
        month = data["months"][0]
        self.assertEqual(month["days"][4]["total_tokens"], 100)
        self.assertEqual(month["days"][5]["total_tokens"], 200)
        self.assertIsNone(month["account_tokens"])
        self.assertEqual(data["timezone"], "UTC")

    def test_partial_week_days_use_observed_growth_and_share_the_weekly_composition(self):
        start = timestamp(date(2026, 10, 5)) + 3600
        midnight = timestamp(date(2026, 10, 6))
        weekly = {"window_started_at": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                  "resets_at": datetime.fromtimestamp(start + 7 * 86400, timezone.utc).isoformat()}
        breakdown = {"basis": "observed_delta", "account_tokens": 500, "attributed_session_tokens": 100,
                     "user_tokens": [{"node_key": "a", "tokens": 100}], "untracked_tokens": 400,
                     "comparable": True, "participant_count": 1, "is_lower_bound": True}
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            db.execute("CREATE TABLE collector_session_usage (node_key TEXT, session_key TEXT, bucket_at INTEGER, tokens_used INTEGER)")
            db.execute("CREATE TABLE account_token_samples (account_key TEXT, sampled_at INTEGER, lifetime_tokens INTEGER)")
            db.executemany("INSERT INTO account_token_samples VALUES (?,?,?)", [
                ("account", start - 10, 0), ("other", start + 10, 99999),
                ("account", start + 10, 1000), ("account", midnight - 10, 1200),
                ("account", midnight + 10, 1300), ("account", self.now, 1500),
                ("account", self.now + 10, 99999),
            ])
            db.execute("INSERT INTO collector_session_usage VALUES ('a', 's', ?, 50)", (start + 60,))
            account = {"account_key": "account", "daily_usage": [{"date": "2026-10-05", "tokens": 99999}]}
            report = read_analytics(db, account, weekly, [], [{"key": "a"}], self.now, breakdown)["weekly"]
        first, today = report["days"][:2]
        self.assertEqual(first["account_tokens"], 200)
        self.assertEqual(first["untracked_tokens"], 150)
        self.assertEqual(first["account_basis"], "observed_delta")
        self.assertEqual(today["account_tokens"], 200)
        self.assertEqual(today["untracked_tokens"], 200)
        # The 100-token increment across midnight has no known date. It is
        # present only in the canonical weekly overview, never split or lost.
        self.assertEqual(report["account_tokens"], 400)
        self.assertEqual(report["overview"]["account_tokens"], 500)
        self.assertEqual(report["overview"]["users"], {"a": 100})
        self.assertEqual(report["overview"]["untracked_tokens"], 400)
        self.assertTrue(all(day["account_tokens"] is None for day in report["days"][2:]))

    def test_daily_reports_are_not_added_to_overlapping_counter_growth(self):
        window = UsageWindow(timestamp(date(2026, 10, 5)), self.now)
        report = self.report({"2026-10-05": 500}, window=window)
        observed = calendar_report(self.start, self.end, self.now, {"2026-10-05": 500}, [], [], {"a"}, window,
                                   {"2026-10-05": 300, "2026-10-06": 200})
        self.assertEqual(report["days"][4]["account_tokens"], 500)
        self.assertEqual(observed["days"][4]["account_tokens"], 500)
        self.assertEqual(observed["days"][4]["account_basis"], "daily_usage")
        self.assertEqual(observed["days"][5]["account_tokens"], 200)

    def test_single_or_decreasing_counter_samples_do_not_invent_daily_usage(self):
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            db.execute("CREATE TABLE account_token_samples (account_key TEXT, sampled_at INTEGER, lifetime_tokens INTEGER)")
            at = timestamp(date(2026, 10, 5))
            db.executemany("INSERT INTO account_token_samples VALUES ('account',?,?)", [
                (at + 1, 100), (at + 2, 90), (at + 86400, 200),
            ])
            self.assertEqual(observed_account_days(db, "account", UsageWindow(at, self.now)), {})
