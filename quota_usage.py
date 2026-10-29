"""Subscription quota accounting, measured in percentage points of the limit.

Token counters describe throughput, not subscription cost. Only provider limit
samples establish quota consumption. Unresolved usage stays at account scope;
no token-based extrapolation or transfer of another model/user rate is valid.
"""

from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

from usage_aggregation import UsageWindow, utc_iso


def reconcile_quota_usage(quota, model_usage, db=None, account_key=None):
    """Retain unallocated usage; model estimates never expand to fill the account meter."""
    if not quota:
        return
    rows = model_usage.get("scopes", {}).get("weekly", {}).get("rows", [])
    weights = {}
    credits_by_node = {}
    tokens_by_node = {}
    unpriced_by_node = {}
    for row in rows:
        node = row["node_key"]
        tokens_by_node[node] = tokens_by_node.get(node, 0) + row["tokens"]
        unpriced_by_node[node] = unpriced_by_node.get(node, 0) + row.get("reference_unpriced_tokens", 0)
        if row.get("standard_reference_credits") is not None:
            credits_by_node[row["node_key"]] = credits_by_node.get(row["node_key"], 0) + row["standard_reference_credits"]
        amount = row.get("estimated_quota_percent")
        if amount is not None and amount > 0:
            weights[row["node_key"]] = weights.get(row["node_key"], 0) + amount
    users = {row["node_key"]: row for row in quota["user_usage"]}
    for node in weights.keys() - users.keys():
        user = {"node_key": node, "tokens": sum(r["tokens"] for r in rows if r["node_key"] == node),
                "used_percent": None, "observed_used_percent": None}
        quota["user_usage"].append(user)
        users[node] = user
    projected = sum(max(user.get("observed_used_percent") or 0, weights.get(node, 0))
                    for node, user in users.items())
    if projected > quota["used_percent"] + 1e-9:
        # A conflicting estimate is invalid, not a share to scale or clip.
        weights = {}
        quota["conversion_status"] = "rejected_account_total_conflict"
        for row in rows:
            row["rejected_quota_percent"] = row.get("estimated_quota_percent")
            row["estimated_quota_percent"] = None
            row["estimated_quota_percent_range"] = None
            row["tokens_per_quota_percent_point"] = None
            row["quota_estimation_basis"] = "invalid_calibration"
    else:
        quota["conversion_status"] = "consistent_with_account_total"
    observed = sum(user.get("observed_used_percent") or 0 for user in users.values())
    weights = {node: max(0.0, value - (users[node].get("observed_used_percent") or 0))
               for node, value in weights.items()}
    for node, user in users.items():
        direct = user.get("observed_used_percent") or 0.0
        estimate = weights.get(node, 0)
        user.update(estimated_used_percent=estimate,
                    counter_based_tokens=user.get("tokens"),
                    tokens=tokens_by_node.get(node),
                    token_basis="weekly_request_details",
                    reference_unpriced_tokens=unpriced_by_node.get(node, 0),
                    standard_reference_credits=credits_by_node.get(node),
                    model_estimated_used_percent=weights.get(node, 0) + direct,
                    used_percent=direct + estimate if direct or node in weights else None,
                    quota_status="estimated" if estimate else "observed" if direct else "unallocated",
                    attribution_reason="관측" if direct and not estimate else "모델 추정")
    quota["observed_percent"] = observed
    quota["estimated_percent"] = sum(user["estimated_used_percent"] for user in users.values())
    quota["attributed_percent"] = sum(user.get("used_percent") or 0 for user in users.values())
    quota["unattributed_percent"] = max(0.0, quota["used_percent"] - quota["attributed_percent"])
    quota["over_estimated_percent"] = max(0.0, quota["attributed_percent"] - quota["used_percent"])
    quota["credits_per_percent_point"] = ((model_usage.get("quota_conversion", {}).get("calibration") or {})
                                          .get("credits_per_percent_point"))
    quota["allocation_basis"] = "observed_plus_calibrated_estimates_with_unallocated_remainder"
    # Largest-remainder rounding makes displayed tenths add to the account total.
    import math
    parts = []
    for node, user in users.items():
        parts.extend([(node, "observed", user.get("observed_used_percent") or 0),
                      (node, "estimated", user["estimated_used_percent"])])
    parts.append((None, "unallocated", quota["unattributed_percent"]))
    units = [math.floor(value * 10) for _, _, value in parts]
    extra = round((quota["used_percent"] + quota["over_estimated_percent"]) * 10) - sum(units)
    for index in sorted(range(len(parts)), key=lambda i: parts[i][2] * 10 - units[i], reverse=True)[:max(0, extra)]:
        units[index] += 1
    for (node, kind, _), value in zip(parts, units):
        if node is None:
            quota["display_unattributed_percent"] = value / 10
        else:
            users[node]["display_" + kind + "_percent"] = value / 10
    for user in users.values():
        user["display_used_percent"] = (user["display_observed_percent"] + user["display_estimated_percent"]
                                         if user["used_percent"] is not None else None)
    quota["display_observed_percent"] = sum(user["display_observed_percent"] for user in users.values())
    quota["display_estimated_percent"] = sum(user["display_estimated_percent"] for user in users.values())
    for node, user in users.items():
        node_rows = [row for row in rows if row["node_key"] == node and row.get("estimated_quota_percent") is not None]
        model_units = [math.floor(row["estimated_quota_percent"] * 10) for row in node_rows]
        extra = round(sum(row["estimated_quota_percent"] for row in node_rows) * 10) - sum(model_units)
        for index in sorted(range(len(node_rows)), key=lambda i: node_rows[i]["estimated_quota_percent"] * 10 - model_units[i], reverse=True)[:max(0, extra)]:
            model_units[index] += 1
        if extra < 0:
            for index in sorted(range(len(node_rows)), key=lambda i: model_units[i], reverse=True):
                reduction = min(model_units[index], -extra)
                model_units[index] -= reduction
                extra += reduction
                if extra == 0:
                    break
        for row, value in zip(node_rows, model_units):
            row["display_estimated_quota_percent"] = value / 10
    if db is not None and quota.get("window_started_at"):
        from model_usage import standard_reference
        start = int(datetime.fromisoformat(quota["window_started_at"].replace("Z", "+00:00")).timestamp())
        daily_weights = {}
        for raw in db.execute("SELECT * FROM collector_usage_details WHERE account_key=? AND bucket_at>=?", (account_key, start)):
            detail = dict(raw)
            credit, _ = standard_reference(detail)
            if credit is not None and credit > 0:
                day = datetime.fromtimestamp(detail["bucket_at"], timezone.utc).date().isoformat()
                weights_for_day = daily_weights.setdefault(day, {})
                weights_for_day[detail["node_key"]] = weights_for_day.get(detail["node_key"], 0) + credit
        for date, day in quota.get("days", {}).items():
            weights_for_day = daily_weights.get(date, {})
            total_weight = sum(weights_for_day.values())
            day["observed_users"] = dict(day["users"])
            day["estimated_users"] = {}
            calibration = model_usage.get("quota_conversion", {}).get("calibration") or {}
            rate = calibration.get("credits_per_percent_point")
            if total_weight and rate:
                for node, weight in weights_for_day.items():
                    amount = max(0.0, weight / rate - day["observed_users"].get(node, 0))
                    day["estimated_users"][node] = amount
                    day["users"][node] = day["users"].get(node, 0) + amount
            day["unattributed_percent"] = max(0.0, day["used_percent"] - sum(day["users"].values()))
            day["over_estimated_percent"] = max(0.0, sum(day["users"].values()) - day["used_percent"])


def account_quota_contract(account_usage, weekly, samples, now):
    """Expose exactly what the authenticated provider reports, without a guessed denominator."""
    limit = next((item for item in (account_usage or {}).get("rate_limits", [])
                  if item.get("id") == weekly["limit_id"]), {})
    recent = []
    for seconds in (300, 900, 3600):
        rows = [row for row in samples if now - seconds <= row["sampled_at"] <= now]
        first, last = (rows[0], rows[-1]) if len(rows) >= 2 else (None, None)
        delta = last["used_percent"] - first["used_percent"] if first is not None else None
        recent.append({
            "requested_seconds": seconds, "sample_count": len(rows),
            "started_at": utc_iso(first["sampled_at"]) if first is not None else None,
            "ended_at": utc_iso(last["sampled_at"]) if last is not None else None,
            "start_percent": first["used_percent"] if first is not None else None,
            "end_percent": last["used_percent"] if last is not None else None,
            "change_percent_points": delta,
            "status": "insufficient_samples" if delta is None else "provider_correction" if delta < 0
                      else "reported_increase" if delta > 0 else "reported_unchanged",
        })
    return {
        "source": "account/rateLimits/read", "scope": "account",
        "plan_code": limit.get("plan"),
        "provided_capacity": {"value": 100, "unit": "provider_percent"},
        "absolute_capacity": {"value": None, "unit": None, "status": "not_reported_by_provider"},
        "used_percent": weekly["used_percent"], "remaining_percent": 100 - weekly["used_percent"],
        "window_minutes": weekly["window_minutes"], "resets_at": weekly["resets_at"],
        "per_request_quota_available": False, "recent_changes": recent,
    }


def build_quota_usage(weekly, samples, events, users, now, account_tokens=None, calendar_days=()):
    if not weekly:
        return None
    window = UsageWindow.weekly(weekly, now)
    used = min(100.0, max(0.0, float(weekly["used_percent"])))
    samples = [dict(row) for row in samples if window.start <= row["sampled_at"] <= window.end]
    # A downward correction within the SAME reset window invalidates earlier
    # attribution. The first sample is a balance, never observed consumption.
    correction = 0
    for index in range(1, len(samples)):
        if samples[index]["used_percent"] < samples[index - 1]["used_percent"]:
            correction = index
    samples = samples[correction:]
    events = sorted((dict(row) for row in events if row["tokens"] > 0), key=lambda row: row["bucket_at"])
    event_times = [row["bucket_at"] for row in events]
    totals, days = {}, {}
    reasons = {"initial_balance": samples[0]["used_percent"] if samples else used,
               "simultaneous_activity": 0.0, "observation_gap": 0.0, "missing_activity": 0.0}
    anchor = samples[0]["sampled_at"] if samples else None
    previous = samples[0] if samples else None
    continuous = True
    for current in samples[1:]:
        start, end = previous["sampled_at"], current["sampled_at"]
        delta = max(0.0, current["used_percent"] - previous["used_percent"])
        start_day = datetime.fromtimestamp(start, timezone.utc).date().isoformat()
        end_day = datetime.fromtimestamp(end, timezone.utc).date().isoformat()
        day = days.setdefault(end_day, {"used_percent": 0.0, "users": {}}) if start_day == end_day else None
        if day is not None:
            day["used_percent"] += delta
        continuous = continuous and end - start <= 120
        if delta > 0:
            # Percentages can be rounded: include activity throughout the flat
            # plateau, not just the final polling interval. Minute buckets and
            # receipt-time sampling add uncertainty at both boundaries.
            candidates = events[bisect_right(event_times, anchor - 60):bisect_left(event_times, end + 30)]
            nodes = {row["node_key"] for row in candidates}
            target = next(iter(nodes)) if len(nodes) == 1 and continuous and end - anchor <= 300 else None
            if target is not None:
                totals[target] = totals.get(target, 0.0) + delta
                # An interval spanning midnight has no provable usage date.
                anchor_day = datetime.fromtimestamp(anchor, timezone.utc).date().isoformat()
                if day is not None and anchor_day == end_day:
                    day["users"][target] = day["users"].get(target, 0.0) + delta
            else:
                reason = "observation_gap" if not continuous or end - anchor > 300 else "simultaneous_activity" if len(nodes) > 1 else "missing_activity"
                reasons[reason] += delta
            anchor, continuous = end, True
        previous = current
    visible = {item["node_key"] for item in users}
    # A correction/latest snapshot must never allow attributed usage > used.
    if sum(totals.values()) > used + 1e-9:
        totals, days = {}, {}
    attributed = sum(value for key, value in totals.items() if key in visible)
    rows = [{**item, "used_percent": totals.get(item["node_key"]), "is_estimate": True} for item in users]
    rows.sort(key=lambda item: (-(item["used_percent"] or 0), item["node_key"]))
    for day in days.values():
        day["users"] = {key: value for key, value in day["users"].items() if key in visible}
        day["unattributed_percent"] = max(0.0, day["used_percent"] - sum(day["users"].values()))
    for row in rows:
        row["observed_used_percent"] = row["used_percent"]
        row["usage_status"] = "recorded" if row["tokens"] > 0 else "no_records"
        row["quota_status"] = "observed_intervals" if row["used_percent"] is not None else "not_attributed"
        row["attribution_reason"] = (
            "계정 한도 증가와 단독 활동이 일치한 구간만 표시 · 주간 전체 사용률 아님"
            if row["used_percent"] is not None else
            "개인별 한도 사용률은 제공되지 않음 · 계정 평균·토큰 비례 배분 제외"
        )
    return {
        "basis": "rate_limit_delta", "unit": "quota_percent", "is_estimate": True,
        "used_percent": used, "remaining_percent": 100.0 - used,
        "attributed_percent": attributed, "unattributed_percent": max(0.0, used - attributed),
        "user_usage": rows, "days": days, "sample_count": len(samples),
        "unattributed_reasons": reasons,
        "coverage_started_at": utc_iso(samples[0]["sampled_at"]) if samples else None,
        "coverage_ended_at": utc_iso(samples[-1]["sampled_at"]) if samples else None,
        "window_started_at": weekly["window_started_at"], "resets_at": weekly["resets_at"],
    }


def read_quota_usage(db, account_key, weekly, users, now, account_usage=None):
    if not weekly:
        return None
    window = UsageWindow.weekly(weekly, now)
    resets_at = int(datetime.fromisoformat(weekly["resets_at"].replace("Z", "+00:00")).timestamp())
    samples = db.execute(
        """SELECT sampled_at, used_percent FROM account_limit_samples
           WHERE account_key=? AND limit_id=? AND window_kind=? AND resets_at=?
           AND window_minutes=? AND sampled_at>=? AND sampled_at<=? ORDER BY sampled_at""",
        (account_key, weekly["limit_id"], weekly["window_kind"], resets_at,
         weekly["window_minutes"], window.start, window.end),
    ).fetchall()
    events = db.execute(
        """SELECT node_key, session_key, bucket_at, tokens_used AS tokens FROM collector_session_usage
           WHERE account_key=? AND bucket_at>=? AND bucket_at+60<=?
           ORDER BY bucket_at""",
        (account_key, window.start, window.end),
    ).fetchall()
    result = build_quota_usage(weekly, samples, events, users, now)
    result["account_quota"] = account_quota_contract(account_usage, weekly, samples, now)
    details = {}
    for row in db.execute(
        """SELECT node_key, model, effort, service_tier, reasoning_mode, SUM(tokens) AS tokens,
           SUM(events) AS events, SUM(input_tokens) AS input_tokens,
           SUM(cached_input_tokens) AS cached_input_tokens, SUM(output_tokens) AS output_tokens,
           SUM(reasoning_output_tokens) AS reasoning_output_tokens,
           COUNT(*) AS buckets, COUNT(input_tokens) AS input_buckets, COUNT(output_tokens) AS output_buckets
           FROM collector_usage_details WHERE account_key=? AND bucket_at>=? AND bucket_at+60<=?
           GROUP BY node_key,model,effort,service_tier,reasoning_mode ORDER BY SUM(tokens) DESC""",
        (account_key, window.start, window.end),
    ):
        details.setdefault(row["node_key"], []).append(dict(row))
    metadata = {row["node_key"]: dict(row) for row in db.execute(
        "SELECT node_key,version,kind,backfill_pending FROM collector_usage_metadata WHERE account_key=?", (account_key,),
    )}
    for user in result["user_usage"]:
        user["usage_details"] = details.get(user["node_key"], [])
        user["collector_metadata"] = metadata.get(user["node_key"], {})
    return result
