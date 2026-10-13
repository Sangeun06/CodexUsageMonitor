"""Shared period attribution rules, independent of HTTP and collector transport.

Counters and timed events overlap: take the larger lower bound per session,
never their sum. An old session's lifetime counter is not a weekly amount.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
from typing import Any

SessionKey = tuple[str, str]


def utc_iso(timestamp: int | float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class UsageWindow:
    start: int
    end: int

    @classmethod
    def weekly(cls, weekly: dict[str, Any], now: int) -> "UsageWindow":
        def timestamp(value: str) -> int:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        return cls(timestamp(weekly["window_started_at"]), min(now, timestamp(weekly["resets_at"])))


class UsageRepository:
    """Read-only queries; the caller owns the connection and its lock."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def verified_sessions(self, window: UsageWindow) -> dict[tuple[str, str], int]:
        return {
            (row["node_key"], row["session_key"]): int(row["tokens"] or 0)
            for row in self.db.execute(
                """SELECT node_key, session_key, SUM(tokens_used) AS tokens
                   FROM collector_session_usage
                   WHERE bucket_at >= ? AND bucket_at + 60 <= ?
                   GROUP BY node_key, session_key""", (window.start, window.end),
            )
        }

    def first_snapshots(self, window: UsageWindow) -> dict[tuple[str, str], tuple[int, int]]:
        # A sample BEFORE the reset could include pre-reset growth. Only a
        # sample at/after the boundary can safely baseline an old session.
        return {
            (row["node_key"], row["session_key"]): (int(row["tokens_used"]), int(row["observed_at"]))
            for row in self.db.execute(
                """SELECT s.node_key, s.session_key, s.tokens_used, s.observed_at
                   FROM session_samples s JOIN (
                       SELECT node_key, session_key, MIN(observed_at) AS observed_at
                       FROM session_samples WHERE observed_at >= ? AND observed_at <= ?
                       GROUP BY node_key, session_key
                   ) b USING (node_key, session_key, observed_at)""", (window.start, window.end),
            )
        }

    def daily_events(self, window: UsageWindow) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT node_key, date(bucket_at, 'unixepoch', 'localtime') AS usage_date,
                      SUM(tokens_used) AS tokens
               FROM collector_session_usage WHERE bucket_at >= ? AND bucket_at + 60 <= ?
               GROUP BY node_key, usage_date""", (window.start, window.end),
        ).fetchall()


@dataclass(frozen=True)
class SessionUsage:
    tokens: int
    event_tokens: int
    snapshot_tokens: int
    maximum_tokens: int
    coverage_start: int

    @property
    def snapshot_only_tokens(self) -> int:
        return self.tokens - self.event_tokens


def attribute_sessions(
    sessions: list[dict[str, Any]], visible_nodes: set[str], window: UsageWindow,
    events: dict[SessionKey, int], baselines: dict[SessionKey, tuple[int, int]],
) -> dict[SessionKey, SessionUsage]:
    """Return conservative period amounts, including archived/event-only sessions."""
    result = {}
    for session in sessions:
        key = (session["node_key"], session["key"])
        if key[0] not in visible_nodes:
            continue
        current = max(0, int(session.get("tokens") or 0))
        created = int(session.get("created_at") or 0)
        updated = int(session.get("updated_at") or 0)
        baseline = baselines.get(key)
        coverage_start = window.start
        snapshot = 0
        possible = 0
        if baseline is not None and baseline[1] == window.start and updated <= window.end:
            # A counter already observed at the boundary is the starting
            # balance, even if the session was created at that same second.
            snapshot = max(0, current - baseline[0])
        elif window.start <= created <= updated <= window.end and created > 0:
            snapshot = current
        elif created < window.start and window.start <= updated <= window.end:
            if baseline is not None:
                snapshot = max(0, current - baseline[0])
                coverage_start = baseline[1]
                possible = max(snapshot, current) if baseline[1] > window.start else snapshot
            else:
                possible = current
        event = max(0, events.get(key, 0))
        tokens = max(snapshot, event)
        result[key] = SessionUsage(tokens, event, snapshot, max(tokens, possible), coverage_start)
    for key, event in events.items():
        if key[0] in visible_nodes and key not in result:
            result[key] = SessionUsage(event, event, 0, event, window.start)
    return result


def aggregate_users(attribution: dict[SessionKey, SessionUsage]) -> list[dict[str, Any]]:
    totals = {}
    for (node, _), usage in attribution.items():
        if usage.tokens > 0:
            totals[node] = totals.get(node, 0) + usage.tokens
    return sorted(
        ({"node_key": node, "tokens": tokens} for node, tokens in totals.items()),
        key=lambda item: (-item["tokens"], item["node_key"]),
    )


def complete_account_days(account_usage: dict[str, Any], window: UsageWindow) -> dict[str, int]:
    daily = {}
    for bucket in account_usage.get("daily_usage", []):
        try:
            day = datetime.strptime(bucket["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            tokens = max(0, int(bucket["tokens"]))
        except (KeyError, TypeError, ValueError):
            continue
        if window.start <= int(day.timestamp()) and int(day.timestamp()) + 86400 <= window.end:
            daily[bucket["date"]] = tokens
    return daily


def build_weekly_breakdown(
    account_usage: dict[str, Any], window: UsageWindow,
    attribution: dict[SessionKey, SessionUsage], period: dict[str, Any],
) -> dict[str, Any]:
    daily = complete_account_days(account_usage, window)
    account_tokens = sum(daily.values()) if daily else (
        period.get("account_tokens") if period.get("account_sample_count", 0) >= 2 else None
    )
    users = aggregate_users(attribution)
    tracked = sum(item["tokens"] for item in users)
    comparable = account_tokens is not None and account_tokens > 0 and tracked <= account_tokens
    return {
        "basis": "daily_lower_bound" if daily else "observed_delta",
        "is_lower_bound": True,
        "coverage_started_at": utc_iso(window.start), "coverage_ended_at": utc_iso(window.end),
        "account_coverage_started_at": utc_iso(window.start) if daily else period.get("coverage_started_at"),
        "account_dates": sorted(daily), "account_tokens": account_tokens,
        "attributed_session_tokens": tracked, "user_tokens": users,
        "verified_event_tokens": sum(item.event_tokens for item in attribution.values()),
        "snapshot_only_tokens": sum(item.snapshot_only_tokens for item in attribution.values()),
        "participant_count": len(users), "comparable": comparable,
        "untracked_tokens": account_tokens - tracked if comparable else None,
    }
