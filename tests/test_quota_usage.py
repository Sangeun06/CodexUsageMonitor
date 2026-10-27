import sqlite3
import unittest

from quota_usage import build_quota_usage, read_quota_usage
from usage_aggregation import utc_iso


class QuotaUsageTest(unittest.TestCase):
    def setUp(self):
        self.weekly = {"limit_id": "codex", "window_kind": "primary", "window_minutes": 10080,
                       "window_started_at": utc_iso(900), "resets_at": utc_iso(900 + 604800),
                       "used_percent": 10}
        self.users = [{"node_key": "cheap", "tokens": 1_000_000}, {"node_key": "expensive", "tokens": 10}]

    def build(self, samples, events=(), used=10):
        return build_quota_usage({**self.weekly, "used_percent": used},
                                 [{"sampled_at": at, "used_percent": value} for at, value in samples],
                                 [{"bucket_at": at, "node_key": node, "tokens": tokens} for at, node, tokens in events],
                                 self.users, 3000)

    def test_more_tokens_do_not_imply_more_quota_and_full_allowance_is_denominator(self):
        result = self.build([(960, 5), (1080, 6), (1200, 10)],
                            [(1020, "cheap", 1_000_000), (1140, "expensive", 10)])
        users = {row["node_key"]: row["used_percent"] for row in result["user_usage"]}
        self.assertEqual(users, {"cheap": 1, "expensive": 4})
        self.assertEqual(result["attributed_percent"], 5)
        self.assertEqual(result["unattributed_percent"], 5)
        self.assertEqual(result["remaining_percent"], 90)
        self.assertEqual(sum((result["attributed_percent"], result["unattributed_percent"], result["remaining_percent"])), 100)

    def test_simultaneous_different_models_are_not_split_by_raw_token_counts(self):
        result = self.build([(960, 5), (1080, 10)],
                            [(1020, "cheap", 1_000_000), (1020, "expensive", 10)])
        self.assertEqual(result["attributed_percent"], 0)
        self.assertTrue(all(user["used_percent"] is None for user in result["user_usage"]))
        self.assertEqual(result["unattributed_percent"], 10)

    def test_first_sample_is_not_assigned_even_if_only_one_user_has_tokens(self):
        result = self.build([(1080, 10)], [(1020, "cheap", 1_000_000)])
        self.assertEqual(result["attributed_percent"], 0)
        self.assertEqual(result["unattributed_percent"], 10)

    def test_rounded_plateau_includes_all_activity_since_last_increase(self):
        result = self.build([(960, 5), (1020, 5), (1080, 5), (1140, 10)],
                            [(960, "cheap", 1_000_000), (1080, "expensive", 10)])
        self.assertEqual(result["attributed_percent"], 0)
        single = self.build([(960, 5), (1020, 5), (1080, 5), (1140, 10)], [(960, "cheap", 1)])
        self.assertEqual(single["attributed_percent"], 5)

    def test_polling_gaps_and_long_plateaus_are_unattributed(self):
        for samples in ([(960, 5), (1260, 10)], [(960, 5), (1080, 5), (1200, 5), (1320, 10)]):
            result = self.build(samples, [(1200, "cheap", 100)])
            self.assertEqual(result["attributed_percent"], 0)

    def test_downward_correction_discards_prior_inferences_and_negative_deltas(self):
        result = self.build([(960, 0), (1080, 50), (1200, 4), (1320, 10)],
                            [(1020, "cheap", 999), (1260, "expensive", 10)])
        self.assertEqual(result["attributed_percent"], 6)
        self.assertEqual(result["unattributed_percent"], 4)
        self.assertIsNone(next(user for user in result["user_usage"] if user["node_key"] == "cheap")["used_percent"])

    def test_unknown_nodes_do_not_inflate_visible_users(self):
        result = self.build([(960, 5), (1080, 10)], [(1020, "hidden", 100)])
        self.assertEqual(result["attributed_percent"], 0)
        self.assertEqual(result["unattributed_percent"], 10)

    def test_missing_samples_or_events_never_fall_back_to_tokens(self):
        for samples in ([], [(960, 5), (1080, 10)]):
            result = self.build(samples)
            self.assertEqual(result["attributed_percent"], 0)
            self.assertEqual(result["unattributed_percent"], 10)

    def test_zero_usage_retains_all_remaining_capacity(self):
        result = self.build([(960, 0), (1080, 0)], [(1020, "cheap", 100)], used=0)
        self.assertEqual(result["remaining_percent"], 100)
        self.assertEqual(result["unattributed_percent"], 0)
        self.assertEqual(result["days"]["1970-01-01"]["used_percent"], 0)

    def test_unobserved_usage_is_never_forced_onto_token_heavy_low_model_user(self):
        result = self.build([(1080, 10)])
        for user in result["user_usage"]:
            self.assertIsNone(user["used_percent"])
            self.assertNotIn("estimated_used_percent", user)
        self.assertNotIn("estimated_attributed_percent", result)
        self.assertEqual(result["unattributed_percent"], 10)

    def test_solo_observation_is_not_extrapolated_to_other_activity_or_users(self):
        result = self.build([(960, 0), (1080, 2), (1200, 10)],
            [(1020, "cheap", 100), (1140, "cheap", 1_000_000), (1140, "expensive", 10)])
        rows = {row["node_key"]: row for row in result["user_usage"]}
        self.assertEqual(rows["cheap"]["used_percent"], 2)
        self.assertIsNone(rows["expensive"]["used_percent"])
        self.assertEqual(result["unattributed_percent"], 8)
        self.assertIn("주간 전체 사용률 아님", rows["cheap"]["attribution_reason"])

    def test_midnight_growth_has_no_invented_daily_date(self):
        weekly = {**self.weekly, "used_percent": 10}
        result = build_quota_usage(weekly, [{"sampled_at": 86340, "used_percent": 5},
                                          {"sampled_at": 86460, "used_percent": 10}],
                                  [{"bucket_at": 86400, "node_key": "cheap", "tokens": 10}], self.users, 86580)
        self.assertEqual(result["attributed_percent"], 5)
        self.assertEqual(result["days"], {})

    def test_reader_isolates_account_limit_kind_reset_and_complete_events(self):
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            db.execute("CREATE TABLE account_limit_samples (account_key, limit_id, window_kind, resets_at, window_minutes, sampled_at, used_percent)")
            db.execute("CREATE TABLE collector_session_usage (account_key, node_key, session_key, bucket_at, tokens_used)")
            db.execute("CREATE TABLE collector_usage_details (account_key,node_key,model,effort,service_tier,reasoning_mode,bucket_at,tokens,events,input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens)")
            db.execute("CREATE TABLE collector_usage_metadata (account_key,node_key,version,backfill_pending)")
            reset = 900 + 604800
            db.executemany("INSERT INTO account_limit_samples VALUES (?,?,?,?,?,?,?)", [
                ("a", "codex", "primary", reset, 10080, 960, 5),
                ("a", "codex", "primary", reset, 10080, 1080, 10),
                ("b", "codex", "primary", reset, 10080, 1020, 95),
                ("a", "other", "primary", reset, 10080, 1020, 95),
                ("a", "codex", "secondary", reset, 10080, 1020, 95),
                ("a", "codex", "primary", reset + 1, 10080, 1020, 95),
                ("a", "codex", "primary", reset, 10080, 1200, 99),
            ])
            db.executemany("INSERT INTO collector_session_usage VALUES (?,?,?,?,?)", [
                ("a", "cheap", "s", 1020, 100), ("b", "expensive", "s", 1020, 999),
                ("a", "expensive", "s", 1080, 999),  # incomplete minute
            ])
            result = read_quota_usage(db, "a", self.weekly, self.users, 1080)
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["attributed_percent"], 5)
        self.assertEqual(result["user_usage"][0]["node_key"], "cheap")
