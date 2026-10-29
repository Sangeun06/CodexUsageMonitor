"""Project/model token evidence and a published Standard-speed credit reference.

Credit rates are not the included subscription budget. Never normalize these
values against the account quota, or treat a session's latest model as history.
"""

from datetime import datetime
from bisect import bisect_left, bisect_right
from statistics import median, quantiles

from usage_aggregation import UsageWindow, utc_iso


RATE_SOURCE = "https://learn.chatgpt.com/docs/pricing#token-rates"
RATE_VERIFIED = "2026-10-28"
# Credits per million tokens: uncached input, cached input, output (reasoning included).
STANDARD_RATES = {
    "gpt-6-astra": (250, 25, 1250),
    "gpt-6.1-sol": (50, 2.5, 250),
    "gpt-6.1-terra": (25, 1.25, 125),
    "gpt-6-sol": (50, 5, 250),
    "gpt-6-luna": (2.5, 0.25, 12.5),
    "gpt-5.6-sol": (100, 10, 500),
    "gpt-5.6-terra": (50, 5, 300),
    "gpt-5.6-luna": (5, 0.5, 30),
}
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def _quota_calibration(db, account_key, weekly, now):
    """Learn a conservative credit-to-quota scale from single-model intervals.

    Provider percentages are rounded. Each positive change is paired with the
    local request details accumulated since the previous change. Mixed model
    or node intervals are excluded; the rate card is only used to compare
    these measured intervals with other models.
    """
    if not weekly:
        return None
    if not weekly.get("limit_id") or not weekly.get("window_minutes"):
        return {"status": "insufficient_clean_windows", "sample_count": 0, "observations": []}
    resets_at = int(datetime.fromisoformat(weekly["resets_at"].replace("Z", "+00:00")).timestamp())
    calibration_start = now - 14 * 86400
    samples = list(db.execute(
        """SELECT resets_at, sampled_at, used_percent FROM account_limit_samples
           WHERE account_key=? AND limit_id=? AND window_kind='primary' AND window_minutes=?
             AND sampled_at>=? AND sampled_at<=? ORDER BY resets_at, sampled_at""",
        (account_key, weekly["limit_id"], weekly["window_minutes"], calibration_start, now),
    ))
    sample_groups = {}
    for row in samples:
        sample_groups.setdefault(row["resets_at"], []).append(row)
    details = [dict(row) for row in db.execute(
        """SELECT node_key, bucket_at, model, effort, service_tier, reasoning_mode,
                  tokens, input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens
           FROM collector_usage_details WHERE account_key=? AND bucket_at>=? AND bucket_at+60<=?
           ORDER BY bucket_at""", (account_key, calibration_start, now),
    )]
    times = [row["bucket_at"] for row in details]
    weekly_window = UsageWindow.weekly(weekly, now)
    weekly_credits = sum(standard_reference(row)[0] or 0 for row in details
                         if weekly_window.start <= row["bucket_at"] and row["bucket_at"] + 60 <= weekly_window.end)
    observations, selected_reset = [], None
    # The provider may roll its reset timestamp during a plan/window change.
    # Prefer the newest historical window with enough clean observations when
    # the newly reported window has not accumulated a usable calibration yet.
    candidates = sorted(
        ((key, rows) for key, rows in sample_groups.items() if len(rows) >= 5),
        key=lambda item: item[1][-1]["sampled_at"], reverse=True,
    )
    for candidate_reset, candidate_samples in candidates:
        candidate_observations = []
        previous = candidate_samples[0]
        for current in candidate_samples[1:]:
            delta = current["used_percent"] - previous["used_percent"]
            elapsed = current["sampled_at"] - previous["sampled_at"]
            if delta < 0:
                previous = current
                continue
            if delta > 0 and elapsed <= 7200:
                left = bisect_left(times, previous["sampled_at"] - 60)
                right = bisect_right(times, current["sampled_at"] + 120)
                interval = details[left:right]
                identities = {(r["node_key"], r["model"], r["effort"]) for r in interval}
                credits = []
                if interval and len(identities) == 1:
                    for row in interval:
                        amount, _ = standard_reference(row)
                        if amount is None:
                            credits = []
                            break
                        credits.append(amount)
                if credits:
                    candidate_observations.append({
                        "node_key": interval[0]["node_key"], "model": interval[0]["model"],
                        "effort": interval[0]["effort"], "quota_percent": delta,
                        "tokens": sum(row["tokens"] for row in interval),
                        "credits": sum(credits), "seconds": elapsed,
                    })
            if delta > 0:
                previous = current
        if len(candidate_observations) >= 5:
            candidate_rates = [row["credits"] / row["quota_percent"] for row in candidate_observations]
            lower, _, upper = quantiles(candidate_rates, n=4, method="inclusive")
            candidate_rate = median(candidate_rates)
            # Sparse/misaligned detail windows can produce tiny denominators.
            # Reject unstable candidates and those contradicting the provider
            # meter. Never inflate or rescale user usage to fit that meter.
            if lower <= 0 or upper > lower * 2:
                continue
            used = weekly.get("used_percent")
            if used is not None and weekly_credits / candidate_rate > used + 1e-9:
                continue
            observations, selected_reset = candidate_observations, candidate_reset
            break
    if len(observations) < 5:
        return {"status": "insufficient_clean_windows", "sample_count": len(observations),
                "observations": observations}
    rates = [row["credits"] / row["quota_percent"] for row in observations]
    ordered = sorted(rates)
    quartiles = quantiles(ordered, n=4, method="inclusive")
    return {
        "status": "measured_single_model_windows" if abs(selected_reset - resets_at) <= 120
                  else "historical_single_model_windows",
        "sample_count": len(observations),
        "period_ended_at": utc_iso(max(row["sampled_at"] for row in sample_groups[selected_reset])),
        "credits_per_percent_point": median(rates),
        "observed_range": {"low": min(rates), "high": max(rates)},
        "middle_50_percent": {"low": quartiles[0], "high": quartiles[2]},
        "observations": observations,
    }


def standard_reference(row):
    """Return a reference cost only for an explicitly priced model and valid counts."""
    rates = STANDARD_RATES.get(row["model"])
    if rates is None:
        return None, "model_rate_unpublished"
    values = [row.get(field) for field in TOKEN_FIELDS[:3]]
    if any(not isinstance(value, int) or value < 0 for value in values):
        return None, "token_components_missing"
    input_tokens, cached, output = values
    if cached > input_tokens or input_tokens + output != row["tokens"]:
        return None, "token_components_inconsistent"
    return ((input_tokens - cached) * rates[0] + cached * rates[1] + output * rates[2]) / 1_000_000, "priced"


def read_model_usage(db, account_key, weekly, now):
    """Join exact account/node/session identities, retaining archived sessions too."""
    start = (now - 14 * 86400) // 60 * 60
    weekly_window = UsageWindow.weekly(weekly, now) if weekly else None
    scopes = {
        "weekly": {"started_at": utc_iso(weekly_window.start) if weekly_window else None,
                   "ended_at": utc_iso(weekly_window.end) if weekly_window else None},
        "recovered": {"started_at": utc_iso(start), "ended_at": utc_iso(now)},
    }
    groups = {name: {} for name in scopes}
    query = """SELECT d.*, s.project_key, s.project_name
               FROM collector_usage_details d LEFT JOIN collector_sessions s
               ON s.account_key=d.account_key AND s.node_key=d.node_key AND s.session_key=d.session_key
               WHERE d.account_key=? AND d.bucket_at>=? AND d.bucket_at+60<=?
               ORDER BY d.bucket_at"""
    # A longer subscription window can extend beyond the recovered view.
    query_start = min(start, weekly_window.start) if weekly_window else start
    for raw in db.execute(query, (account_key, query_start, now)):
        detail = dict(raw)
        credit, status = standard_reference(detail)
        for scope, grouped in groups.items():
            in_window = (weekly_window is not None and weekly_window.start <= detail["bucket_at"]
                         and detail["bucket_at"] + 60 <= weekly_window.end) if scope == "weekly" else detail["bucket_at"] >= start
            if not in_window:
                continue
            key = tuple(detail.get(field) for field in
                        ("node_key", "project_key", "model", "effort", "service_tier", "reasoning_mode"))
            if key not in grouped:
                grouped[key] = {
                    **{field: detail.get(field) for field in
                       ("node_key", "project_key", "project_name", "model", "effort", "service_tier", "reasoning_mode")},
                    "tokens": 0, "events": 0, "buckets": 0,
                    "first_at": utc_iso(detail["bucket_at"]), "last_at": None,
                    **{field: 0 for field in TOKEN_FIELDS},
                    "standard_reference_credits": None, "reference_priced_tokens": 0,
                    "reference_unpriced_tokens": 0, "reference_missing_reasons": {},
                    "quota_percent": None,
                }
            row = grouped[key]
            row["tokens"] += detail["tokens"]
            row["events"] += detail["events"]
            row["buckets"] += 1
            row["last_at"] = utc_iso(detail["bucket_at"] + 60)
            for field in TOKEN_FIELDS:
                # Missing components must never silently become zero or a partial total.
                row[field] = row[field] + detail[field] if row[field] is not None and detail[field] is not None else None
            if credit is not None:
                row["standard_reference_credits"] = (row["standard_reference_credits"] or 0) + credit
                row["reference_priced_tokens"] += detail["tokens"]
            else:
                row["reference_unpriced_tokens"] += detail["tokens"]
                reasons = row["reference_missing_reasons"]
                reasons[status] = reasons.get(status, 0) + detail["tokens"]
    calibration = _quota_calibration(db, account_key, weekly, now)
    observed = calibration.get("observations", []) if calibration else []
    direct_rates = {}
    for row in observed:
        direct_rates.setdefault((row["model"], row["effort"]), []).append(
            row["tokens"] / row["quota_percent"]
        )
    direct_rate_summary = []
    for (model, effort), rates in sorted(direct_rates.items()):
        if len(rates) >= 3:
            direct_rate_summary.append({
                "model": model, "effort": effort, "sample_count": len(rates),
                "tokens_per_percent_point": median(rates),
                "observed_range": {"low": min(rates), "high": max(rates)},
            })
    for scope, grouped in groups.items():
        rows = sorted(grouped.values(), key=lambda row: -row["tokens"])
        credits = [row["standard_reference_credits"] for row in rows if row["standard_reference_credits"] is not None]
        for row in rows:
            row["reference_status"] = "partial" if row["reference_priced_tokens"] and row["reference_unpriced_tokens"] else "complete" if row["standard_reference_credits"] is not None else "unavailable"
            row["estimated_quota_percent"] = None
            row["estimated_quota_percent_range"] = None
            row["tokens_per_quota_percent_point"] = None
            row["quota_estimation_basis"] = "unavailable"
            if (scope == "weekly" and calibration and calibration.get("credits_per_percent_point")
                    and row["standard_reference_credits"] is not None):
                row["estimated_quota_percent"] = (
                    row["standard_reference_credits"] / calibration["credits_per_percent_point"]
                )
                band = calibration.get("middle_50_percent") or {}
                if band.get("low") and band.get("high"):
                    row["estimated_quota_percent_range"] = {
                        "low": row["standard_reference_credits"] / band["high"],
                        "high": row["standard_reference_credits"] / band["low"],
                    }
                row["tokens_per_quota_percent_point"] = (
                    row["tokens"] / row["standard_reference_credits"]
                    * calibration["credits_per_percent_point"]
                    if row["standard_reference_credits"] else None
                )
                direct = direct_rates.get((row["model"], row["effort"]), [])
                row["quota_estimation_basis"] = (
                    "direct_model_observation" if len(direct) >= 3
                    else "standard_rate_extrapolation"
                )
        scopes[scope].update({
            "rows": rows, "tokens": sum(row["tokens"] for row in rows),
            "standard_reference_credits": sum(credits) if credits else None,
            "reference_priced_tokens": sum(row["reference_priced_tokens"] for row in rows),
            "reference_unpriced_tokens": sum(row["reference_unpriced_tokens"] for row in rows),
        })
    return {
        "scopes": scopes,
        "rate_card": {
            "source": RATE_SOURCE, "verified_at": RATE_VERIFIED, "unit": "credits_per_million_tokens",
            "speed": "standard", "rates": [
                {"model": model, "input": rates[0], "cached_input": rates[1], "output": rates[2]}
                for model, rates in STANDARD_RATES.items()
            ],
        },
        "quota_conversion": {
            "status": calibration["status"] if calibration else "no_weekly_limit",
            "method": "median_standard_reference_credits_per_observed_quota_point",
            "formula": "estimated_percent = model_standard_reference_credits / measured_credits_per_percent_point",
            "calibration": ({key: value for key, value in calibration.items() if key != "observations"}
                            if calibration else None),
            "direct_model_rates": direct_rate_summary,
            "model_token_formula": "tokens_per_1_percent = model_tokens / model_standard_reference_credits * measured_credits_per_percent_point",
            "included_capacity_credits": None,
            "quota_percent": None,
        },
    }
