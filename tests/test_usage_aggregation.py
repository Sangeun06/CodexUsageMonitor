import unittest

from usage_aggregation import UsageWindow, aggregate_users, attribute_sessions, build_weekly_breakdown


class AttributionTest(unittest.TestCase):
    def session(self, key="s", tokens=100, created=1100, updated=1500, node="a", **extra):
        return dict(key=key, node_key=node, tokens=tokens, created_at=created, updated_at=updated, **extra)

    def attribute(self, sessions, events=None, baselines=None):
        return attribute_sessions(sessions, {"a", "b"}, UsageWindow(1000, 2000), events or {}, baselines or {})

    def test_new_sessions_without_events_and_archived_sessions_are_included(self):
        result = self.attribute([self.session(tokens=200), self.session(key="archive", tokens=50, archived=True)])
        self.assertEqual(aggregate_users(result), [{"node_key": "a", "tokens": 250}])
        self.assertEqual(result[("a", "s")].snapshot_only_tokens, 200)

    def test_old_lifetime_is_not_a_weekly_amount(self):
        result = self.attribute([self.session(tokens=1_000_000, created=800)])
        self.assertEqual(aggregate_users(result), [])
        self.assertEqual(result[("a", "s")].maximum_tokens, 1_000_000)

    def test_old_session_counts_only_observed_growth(self):
        result = self.attribute([self.session(tokens=1_000_050, created=800)], baselines={("a", "s"): (1_000_000, 1200)})
        self.assertEqual(result[("a", "s")].tokens, 50)

    def test_overlapping_sources_merge_per_session_not_per_node(self):
        result = self.attribute(
            [self.session(tokens=200), self.session(key="other", tokens=100)],
            events={("a", "s"): 50, ("a", "other"): 300},
        )
        self.assertEqual(aggregate_users(result), [{"node_key": "a", "tokens": 500}])
        self.assertEqual(sum(item.event_tokens for item in result.values()), 350)
        self.assertEqual(sum(item.snapshot_only_tokens for item in result.values()), 150)

    def test_matching_session_ids_on_different_nodes_are_independent(self):
        result = self.attribute([self.session(tokens=100), self.session(node="b", tokens=200)])
        self.assertEqual(aggregate_users(result), [{"node_key": "b", "tokens": 200}, {"node_key": "a", "tokens": 100}])

    def test_event_only_sessions_survive_and_hidden_nodes_stay_hidden(self):
        result = self.attribute([self.session(node="hidden")], events={("b", "deleted"): 75, ("hidden", "s"): 999})
        self.assertEqual(aggregate_users(result), [{"node_key": "b", "tokens": 75}])

    def test_future_or_pre_window_sessions_do_not_leak_into_week(self):
        result = self.attribute([
            self.session(key="future", created=2100, updated=2200),
            self.session(key="old", created=100, updated=900),
            self.session(key="future-counter", updated=2200),
        ])
        self.assertEqual(aggregate_users(result), [])

    def test_baseline_at_boundary_is_not_counted_as_growth(self):
        result = self.attribute([self.session(created=1000, tokens=120)], baselines={("a", "s"): (100, 1000)})
        self.assertEqual(result[("a", "s")].tokens, 20)

    def test_counter_decrease_does_not_create_negative_usage(self):
        result = self.attribute([self.session(created=800, tokens=50)], baselines={("a", "s"): (100, 1200)})
        self.assertEqual(result[("a", "s")].tokens, 0)

    def test_missing_account_total_does_not_hide_new_users_or_invent_shares(self):
        attribution = self.attribute([self.session()])
        result = build_weekly_breakdown({}, UsageWindow(1000, 2000), attribution, {})
        self.assertEqual(result["participant_count"], 1)
        self.assertEqual(result["attributed_session_tokens"], 100)
        self.assertFalse(result["comparable"])
        self.assertIsNone(result["untracked_tokens"])

    def test_account_fallback_does_not_switch_user_attribution_period(self):
        attribution = self.attribute([self.session()])
        result = build_weekly_breakdown({}, UsageWindow(1000, 2000), attribution, {
            "account_sample_count": 2, "account_tokens": 500,
            "coverage_started_at": "1970-01-01T00:25:00Z", "user_tokens": [],
        })
        self.assertEqual(result["attributed_session_tokens"], 100)
        self.assertEqual(result["untracked_tokens"], 400)
        self.assertTrue(result["is_lower_bound"])
