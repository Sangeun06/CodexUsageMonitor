"""Calendar reports with explicit UTC dates and missing-data semantics."""
from datetime import datetime, timedelta, timezone

from usage_aggregation import UsageWindow, complete_account_days, utc_iso

EVENT_RETENTION_DAYS = 93


def date_of(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).date()


def timestamp(day):
    return int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp())


def next_month(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def read_events(db, window, account_key=None):
    clause = " AND account_key = ?" if account_key is not None else ""
    args = (account_key,) if account_key is not None else ()
    return db.execute(
        f"""SELECT node_key, session_key, date(bucket_at, 'unixepoch') AS date,
                  SUM(tokens_used) AS tokens
           FROM collector_session_usage WHERE bucket_at >= ? AND bucket_at + 60 <= ? {clause}
           GROUP BY node_key, session_key, date""", (window.start, window.end, *args),
    ).fetchall()


def observed_account_days(db, account_key, window):
    """Counter growth between samples within one UTC day is a dated lower bound.

    Growth across midnight has no known date and stays in the weekly overview.
    """
    if not account_key:
        return {}
    samples = {}
    for row in db.execute(
        """SELECT sampled_at, lifetime_tokens FROM account_token_samples
           WHERE account_key = ? AND sampled_at >= ? AND sampled_at <= ?
           ORDER BY sampled_at""", (account_key, window.start, window.end),
    ):
        samples.setdefault(date_of(row["sampled_at"]).isoformat(), []).append(row["lifetime_tokens"])
    return {
        day: values[-1] - values[0] for day, values in samples.items()
        if len(values) >= 2 and all(after >= before for before, after in zip(values, values[1:]))
    }


def calendar_report(start, end, now, account_days, events, sessions, visible, window=None, observed_days=None):
    """Never distribute multi-day counters onto guessed dates or turn null into zero."""
    window = window or UsageWindow(timestamp(start), min(timestamp(end), now))
    per_session = {}
    for event in events:
        if event["node_key"] in visible and start.isoformat() <= event["date"] < end.isoformat():
            per_session[(event["date"], event["node_key"], event["session_key"])] = [int(event["tokens"]), 0]
    for session in sessions:
        created, updated = int(session.get("created_at") or 0), int(session.get("updated_at") or 0)
        if session["node_key"] not in visible or not (0 < window.start <= created <= updated <= window.end):
            continue
        day = date_of(created)
        if day != date_of(updated) or not start <= day < end:
            continue
        key = (day.isoformat(), session["node_key"], session["key"])
        values = per_session.setdefault(key, [0, 0])
        values[1] = max(0, int(session.get("tokens") or 0))
    users_by_day, supplements = {}, {}
    for (day, node, _), (event, snapshot) in per_session.items():
        tokens = max(event, snapshot)
        if tokens:
            users = users_by_day.setdefault(day, {})
            users[node] = users.get(node, 0) + tokens
            supplements[day] = supplements.get(day, 0) + max(0, snapshot - event)
    days = []
    day = start
    today = date_of(now)
    while day < end:
        key = day.isoformat()
        future = day > today
        boundary = timestamp(day) < window.start or min(timestamp(day + timedelta(days=1)), now) > window.end
        account = account_days.get(key) if not future and not boundary else None
        basis = "daily_usage" if account is not None else None
        if account is None and not future and key in (observed_days or {}):
            account = observed_days[key]
            basis = "observed_delta"
        users = users_by_day.get(key, {}) if not future else {}
        tracked = sum(users.values())
        comparable = account is not None and account >= tracked
        days.append({
            "date": key, "users": users, "total_tokens": tracked, "account_tokens": account,
            "account_basis": basis,
            "untracked_tokens": account - tracked if comparable else None,
            "comparable": comparable, "participant_count": len(users),
            "snapshot_only_tokens": supplements.get(key, 0),
            "status": "future" if future else "partial" if day == today or boundary else "reported" if account is not None else "missing",
            "day_type": "weekend" if day.weekday() >= 5 else "weekday",
        })
        day += timedelta(days=1)
    reported = [day for day in days if day["account_tokens"] is not None]
    return {
        "start": start.isoformat(), "end_exclusive": end.isoformat(), "timezone": "UTC", "days": days,
        "account_tokens": sum(day["account_tokens"] for day in reported) if reported else None,
        "reported_days": len(reported), "elapsed_days": sum(day["status"] != "future" for day in days),
        "attributed_tokens": sum(day["total_tokens"] for day in days),
        "participant_count": len({key for day in days for key in day["users"]}),
    }


def read_analytics(db, account_usage, weekly, sessions, nodes, now, weekly_breakdown=None, account_key=None):
    today = date_of(now)
    account_days = {}
    for item in (account_usage or {}).get("daily_usage", []):
        try:
            day = datetime.strptime(item["date"], "%Y-%m-%d").date()
            if day <= today:
                account_days[day.isoformat()] = max(0, int(item["tokens"]))
        except (KeyError, TypeError, ValueError):
            continue
    visible = {node["key"] for node in nodes}
    cutoff = timestamp(today.replace(day=1) - timedelta(days=EVENT_RETENTION_DAYS))
    events = read_events(db, UsageWindow(cutoff, now), account_key)
    month_ids = {today.strftime("%Y-%m"), *(day[:7] for day in account_days)}
    month_ids.update(event["date"][:7] for event in events if event["node_key"] in visible)
    months = []
    for month in sorted(month_ids, reverse=True)[:12]:
        start = datetime.strptime(month, "%Y-%m").date()
        report = calendar_report(start, next_month(start), now, account_days, events, sessions, visible)
        report["id"] = month
        months.append(report)
    weekly_report = None
    if weekly:
        window = UsageWindow.weekly(weekly, now)
        full_end = int(datetime.fromisoformat(weekly["resets_at"].replace("Z", "+00:00")).timestamp())
        end = date_of(full_end - 1) + timedelta(days=1)
        weekly_days, observed_days = account_days, {}
        if weekly_breakdown is not None:
            # Use the same account source as the weekly composition. Full-day
            # reports and observed counters overlap and must never be added.
            weekly_days = complete_account_days(account_usage or {}, window)
            if weekly_breakdown["basis"] == "observed_delta":
                observed_days = observed_account_days(db, (account_usage or {}).get("account_key"), window)
        weekly_report = calendar_report(date_of(window.start), end, now, weekly_days,
                                        read_events(db, window, account_key), sessions, visible, window, observed_days)
        weekly_report["window_started_at"] = utc_iso(window.start)
        weekly_report["window_ended_at"] = utc_iso(window.end)
        if weekly_breakdown is not None:
            weekly_report["overview"] = {
                "date": "__weekly", "scope": "weekly",
                "users": {item["node_key"]: item["tokens"] for item in weekly_breakdown["user_tokens"]},
                "total_tokens": weekly_breakdown["attributed_session_tokens"],
                "account_tokens": weekly_breakdown["account_tokens"],
                "untracked_tokens": weekly_breakdown["untracked_tokens"],
                "comparable": weekly_breakdown["comparable"],
                "participant_count": weekly_breakdown["participant_count"],
                "is_lower_bound": weekly_breakdown["is_lower_bound"],
                "account_basis": weekly_breakdown["basis"],
            }
    return {"timezone": "UTC", "months": months, "weekly": weekly_report,
            "event_retention_days": EVENT_RETENTION_DAYS}
