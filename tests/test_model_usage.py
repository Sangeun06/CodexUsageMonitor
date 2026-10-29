import sqlite3
import unittest

from model_usage import read_model_usage
from usage_aggregation import utc_iso


class ModelUsageTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE collector_sessions (account_key,node_key,session_key,project_key,project_name,model,archived)")
        self.db.execute("""CREATE TABLE collector_usage_details (
            account_key,node_key,session_key,bucket_at,model,effort,service_tier,reasoning_mode,
            tokens,events,input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens)""")
        self.now = 2_000_040
        self.weekly = {"window_started_at": utc_iso(self.now - 7 * 86400),
                       "resets_at": utc_iso(self.now + 60)}

    def session(self, account="a", node="n", session="s", project="p", archived=0):
        self.db.execute("INSERT INTO collector_sessions VALUES (?,?,?,?,?,?,?)",
                        (account, node, session, project, "Project " + project, "latest-model-only", archived))

    def detail(self, account="a", node="n", session="s", at=None, model="gpt-6.1-sol", **values):
        row = dict(account_key=account, node_key=node, session_key=session,
                   bucket_at=self.now - 60 if at is None else at, model=model, effort="high",
                   service_tier="unknown", reasoning_mode="unknown", tokens=1_100_000,
                   events=1, input_tokens=1_000_000, cached_input_tokens=900_000,
                   output_tokens=100_000, reasoning_output_tokens=50_000)
        row.update(values)
        self.db.execute("INSERT INTO collector_usage_details VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row.values()))

    def report(self, weekly=True):
        return read_model_usage(self.db, "a", self.weekly if weekly else None, self.now)

    def test_cache_discount_and_reasoning_are_counted_once(self):
        self.detail()
        row = self.report()["scopes"]["weekly"]["rows"][0]
        # 100k uncached * 50 + 900k cached * 2.5 + 100k output * 250.
        self.assertAlmostEqual(row["standard_reference_credits"], 32.25)
        self.assertEqual(row["tokens"], 1_100_000)
        self.assertEqual(row["reasoning_output_tokens"], 50_000)
        self.assertIsNone(row["quota_percent"])
        self.assertEqual(row["reference_status"], "complete")

    def test_tiny_new_reference_cannot_replace_history_when_it_exceeds_account_meter(self):
        from model_usage import _quota_calibration
        self.db.execute("CREATE TABLE account_limit_samples (account_key,limit_id,window_kind,window_minutes,resets_at,sampled_at,used_percent)")
        weekly = {**self.weekly, "limit_id": "codex", "window_minutes": 10080, "used_percent": 70}
        reset = self.now + 60
        for rate, start, end in [(115, self.now-7200, reset-86400), (6, self.now-1800, reset)]:
            for index in range(6):
                at = start + index * 300
                self.db.execute("INSERT INTO account_limit_samples VALUES (?,?,?,?,?,?,?)",
                                ('a','codex','primary',10080,end,at,index))
                if index:
                    output = rate * 4000
                    self.detail(at=at-120, tokens=output, input_tokens=0,
                                cached_input_tokens=0, output_tokens=output, reasoning_output_tokens=0)
        calibration = _quota_calibration(self.db, 'a', weekly, self.now)
        self.assertEqual(calibration['credits_per_percent_point'], 115)
        self.assertEqual(calibration['status'], 'historical_single_model_windows')
        # With no valid history, retain unknown instead of accepting the bad rate.
        self.db.execute("DELETE FROM account_limit_samples WHERE resets_at=?", (reset-86400,))
        self.assertNotIn('credits_per_percent_point', _quota_calibration(self.db, 'a', weekly, self.now))

    def test_actual_models_within_session_and_archived_project_are_preserved(self):
        self.session(archived=1)
        self.detail()
        self.detail(model="gpt-6-luna")
        rows = self.report()["scopes"]["weekly"]["rows"]
        self.assertEqual({row["model"] for row in rows}, {"gpt-6.1-sol", "gpt-6-luna"})
        self.assertEqual({row["project_name"] for row in rows}, {"Project p"})
        self.assertAlmostEqual(next(row for row in rows if row["model"] == "gpt-6-luna")["standard_reference_credits"], 1.725)

    def test_project_join_is_isolated_by_account_node_and_session(self):
        self.session()
        self.session(account="b", project="wrong-account")
        self.session(node="other", project="wrong-node")
        self.session(session="other", project="wrong-session")
        self.detail()
        self.detail(account="b")
        report = self.report()["scopes"]["weekly"]
        self.assertEqual(len(report["rows"]), 1)
        self.assertEqual(report["rows"][0]["project_key"], "p")
        self.assertEqual(report["tokens"], 1_100_000)

    def test_complete_minutes_and_weekly_vs_recovered_periods(self):
        self.detail(at=self.now - 60)
        self.detail(at=self.now - 8 * 86400)
        self.detail(at=self.now - 15 * 86400)
        self.detail(at=self.now - 30)  # ongoing minute must not cross the boundary
        self.detail(at=self.now + 60)
        report = self.report()
        self.assertEqual(report["scopes"]["weekly"]["tokens"], 1_100_000)
        self.assertEqual(report["scopes"]["recovered"]["tokens"], 2_200_000)
        self.assertEqual(self.report(weekly=False)["scopes"]["weekly"]["rows"], [])

    def test_unpublished_model_is_visible_but_never_substituted(self):
        self.detail(model="codex-auto-review")
        report = self.report()["scopes"]["weekly"]
        self.assertEqual(report["tokens"], 1_100_000)
        self.assertEqual(report["reference_unpriced_tokens"], 1_100_000)
        self.assertIsNone(report["standard_reference_credits"])
        self.assertIn("model_rate_unpublished", report["rows"][0]["reference_missing_reasons"])

    def test_missing_or_invalid_components_never_produce_complete_cost(self):
        self.detail()
        self.detail(at=self.now - 120, cached_input_tokens=None)
        self.detail(at=self.now - 180, cached_input_tokens=1_000_001)
        self.detail(at=self.now - 240, tokens=1_100_001)
        report = self.report()["scopes"]["weekly"]
        row = report["rows"][0]
        self.assertIsNone(row["cached_input_tokens"])
        self.assertEqual(row["reference_status"], "partial")
        self.assertEqual(report["reference_priced_tokens"], 1_100_000)
        self.assertEqual(report["reference_unpriced_tokens"], 3_300_001)
        self.assertAlmostEqual(row["standard_reference_credits"], 32.25)

    def test_no_extra_effort_or_unknown_speed_multiplier_or_invented_capacity(self):
        self.detail(effort="ultra", service_tier="unknown")
        self.detail(at=self.now - 120, effort="low", service_tier="fast")
        result = self.report()
        for row in result["scopes"]["weekly"]["rows"]:
            self.assertEqual(row["standard_reference_credits"], 32.25)
            self.assertIsNone(row["quota_percent"])
        self.assertEqual(result["rate_card"]["speed"], "standard")
        self.assertIsNone(result["quota_conversion"]["included_capacity_credits"])
        self.assertIsNone(result["quota_conversion"]["quota_percent"])


if __name__ == "__main__":
    unittest.main()
