"""Project/model token evidence and a published Standard-speed credit reference.

Credit rates are not the included subscription budget. Never normalize these
values against the account quota, or treat a session's latest model as history.
"""

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
    for scope, grouped in groups.items():
        rows = sorted(grouped.values(), key=lambda row: -row["tokens"])
        credits = [row["standard_reference_credits"] for row in rows if row["standard_reference_credits"] is not None]
        for row in rows:
            row["reference_status"] = "partial" if row["reference_priced_tokens"] and row["reference_unpriced_tokens"] else "complete" if row["standard_reference_credits"] is not None else "unavailable"
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
            "status": "included_capacity_and_conversion_not_published",
            "included_capacity_credits": None, "quota_percent": None,
        },
    }
