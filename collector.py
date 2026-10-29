#!/usr/bin/env python3
"""Read one user's local Codex metadata and send a sanitized snapshot."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlparse


DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_BACKUPS = 3
APP_SERVER_TIMEOUT_SECONDS = 15
COLLECTOR_VERSION = "0.7.6"
UPDATE_FILE_NAMES = {"collector.py", "collector_windows_machine.py"}


class CollectorRestart(RuntimeError):
    """Raised after a verified update is installed and the process must restart."""


def decode_jwt(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


def digest(kind: str, value: str, token: str) -> str:
    return hashlib.sha256(f"{token}:{kind}:{value}".encode()).hexdigest()[:12]


def attribution_scope(node_key: str, token: str) -> str:
    # A collector-token rotation starts a fresh, conservative attribution
    # baseline instead of claiming old sessions under the current account.
    return hashlib.sha256(f"{token}:attribution:{node_key}".encode()).hexdigest()[:24]


def read_account(auth_file: Path, token: str) -> dict[str, Any]:
    try:
        auth = json.loads(auth_file.read_text())
        tokens = auth.get("tokens") or {}
        claims = decode_jwt(tokens.get("id_token") or "")
        details = claims.get("https://api.openai.com/auth") or {}
        account_id = tokens.get("account_id") or details.get("chatgpt_account_id") or claims.get("sub") or "unknown"
        return {
            "key": digest("account", str(account_id), token),
            "name": claims.get("name") or "Unknown",
            "email": claims.get("email"),
            "email_verified": bool(claims.get("email_verified")),
            "plan": details.get("chatgpt_plan_type"),
            "auth_provider": claims.get("auth_provider"),
            "auth_mode": auth.get("auth_mode"),
        }
    except (OSError, json.JSONDecodeError):
        return {"key": "unknown", "name": "Unknown", "email": None, "plan": None}


def _usage_counts(raw):
    """Canonical token components. Cache is part of input; reasoning is part of output."""
    if not isinstance(raw, dict):
        return None
    fields = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    result = {key: _optional_nonnegative_int(raw.get(key)) for key in fields}
    for field, nested, key in (("cached_input_tokens", "input_tokens_details", "cached_tokens"),
                                ("reasoning_output_tokens", "output_tokens_details", "reasoning_tokens")):
        if result[field] is None and isinstance(raw.get(nested), dict):
            result[field] = _optional_nonnegative_int(raw[nested].get(key))
    if result["total_tokens"] is None and result["input_tokens"] is not None and result["output_tokens"] is not None:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result if result["total_tokens"] is not None else None


def _execution_settings(context, settings, new_turn=False):
    if not isinstance(settings, dict):
        return
    if new_turn:
        for key in ("model", "effort", "reasoning_mode"):
            context.pop(key, None)
    model = settings.get("model")
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", model):
        context["model"] = model
    for field, choices in (("effort", {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}),
                           ("reasoning_mode", {"standard", "pro"})):
        value = settings.get(field, settings.get("reasoning_effort") if field == "effort" else None)
        if value is not None:
            context.pop(field, None)
            if isinstance(value, str) and value in choices:
                context[field] = value
    if "service_tier" in settings:
        context.pop("service_tier", None)
        tier = settings["service_tier"]
        normalized = {"default": "standard", "standard": "standard", "priority": "fast",
                      "fast": "fast", "flex": "flex"}.get(tier) if isinstance(tier, str) else None
        if normalized:
            context["service_tier"] = normalized


def _fork_replay_boundary(path, metadata):
    """Match an explicit fork's leading counters to its local parent's history."""
    parent_id = metadata.get("forked_from_id")
    if not parent_id:
        source = metadata.get("source")
        try:
            parent_id = source["subagent"]["thread_spawn"]["parent_thread_id"]
        except (KeyError, TypeError):
            return 0, []
    if not isinstance(parent_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parent_id):
        return 0, []
    home = next((p.parent for p in path.parents if p.name in {"sessions", "archived_sessions"}), None)
    if home is None:
        return 0, []
    parent = next((p for folder in (home / "sessions", home / "archived_sessions")
                   if folder.is_dir() for p in folder.rglob(f"*{parent_id}*.jsonl") if p != path), None)
    if parent is None:
        return 0, []
    def counters(file, before=None):
        with file.open("rb") as stream:
            while True:
                start = stream.tell()
                line = stream.readline()
                if not line:
                    break
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                    continue
                payload = item["payload"]
                if before and isinstance(item.get("timestamp"), str):
                    try:
                        if datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")) > before:
                            continue
                    except (ValueError, TypeError):
                        continue
                if item.get("type") == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    if isinstance(info, dict):
                        counts = _usage_counts(info.get("last_token_usage")) or _usage_counts(info.get("total_token_usage"))
                        if counts:
                            yield start, stream.tell(), counts
    boundary = 0
    try:
        before = datetime.fromisoformat(metadata["_fork_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        before = None
    parent_rows = counters(parent, before)
    child_rows = counters(path)
    try:
        for (_, child_end, child_usage), (_, _, parent_usage) in zip(child_rows, parent_rows):
            if child_usage != parent_usage:
                break
            boundary = child_end + 1
    finally:
        parent_rows.close()
        child_rows.close()
    compactions = []
    with parent.open("rb") as stream:
        for line in stream:
            try:
                item = json.loads(line)
                if not isinstance(item, dict) or item.get("type") != "token_usage_record":
                    continue
                if before and isinstance(item.get("timestamp"), str) and datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")) > before:
                    continue
                response_id = item.get("payload", {}).get("response_id")
                if isinstance(response_id, str):
                    compactions.append(hashlib.sha256(response_id.encode()).hexdigest())
            except (ValueError, TypeError, AttributeError):
                continue
    return boundary, compactions


def _read_rollout_usage_buckets(
    rollout_path: Any, cutoff: int, offset: int, previous_total: int | None = None,
    details: list[dict[str, Any]] | None = None, context: dict | None = None,
    stop_offset: int | None = None, max_bytes: int | None = None,
) -> tuple[list[dict[str, int]], int, int | None]:
    if not rollout_path or not Path(str(rollout_path)).is_file():
        return [], offset, previous_total
    path = Path(str(rollout_path))
    context = context if context is not None else {}
    buckets = {}
    initial_total = previous_total

    def emit(usage, timestamp, settings):
        if not usage or not usage.get("total_tokens") or not isinstance(timestamp, str):
            return
        try:
            at = int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())
        except (ValueError, OverflowError):
            return
        if at < cutoff:
            return
        at -= at % 60
        tokens = usage["total_tokens"]
        buckets[at] = buckets.get(at, 0) + tokens
        if details is not None:
            details.append({"at": at, "tokens": tokens, "events": 1,
                            **{key: usage.get(key) for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")},
                            **{key: settings.get(key, "unknown") for key in ("model", "effort", "service_tier", "reasoning_mode")}})

    try:
        size = path.stat().st_size
        if offset < 0 or offset > size:
            offset, previous_total = 0, None
            context.clear()
        if offset == 0 and "_replay_boundary" not in context:
            # Decide from actual markers, including markers AFTER a long copied prefix.
            # Store only a byte boundary, never session content or parent prompts.
            context["_replay_boundary"] = 0
            metadata = {}
            with path.open("rb") as probe:
                while line := probe.readline():
                    try:
                        item = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(item, dict) or not isinstance(item.get("payload"), dict) or not isinstance(item.get("type"), str):
                        continue
                    payload = item["payload"]
                    if item.get("type") == "session_meta" and not metadata:
                        metadata = {**payload, "_fork_at": item.get("timestamp")}
                    if ((item.get("type") == "event_msg" and payload.get("type") == "task_started")
                            or (item.get("type") in {"inter_agent_communication_metadata", "inter_agent_communication"}
                                and payload.get("trigger_turn") is True)):
                        context["_replay_boundary"] = probe.tell()
                        break
            if not context["_replay_boundary"] and metadata:
                context["_replay_boundary"], context["_parent_compactions"] = _fork_replay_boundary(path, metadata)
        with path.open("rb") as stream:
            stream.seek(offset)
            while True:
                if stop_offset is not None and stream.tell() >= stop_offset:
                    break
                if max_bytes is not None and stream.tell() - offset >= max_bytes:
                    break
                line_start = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    stream.seek(line_start)
                    break
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict) or not isinstance(event.get("payload"), dict) or not isinstance(event.get("type"), str):
                    continue
                payload, kind = event["payload"], event.get("type")
                replay = stream.tell() < context.get("_replay_boundary", 0)
                if kind == "turn_context":
                    context["_turn"] = context.get("_turn", 0) + 1
                    _execution_settings(context, payload, new_turn=True)
                    continue
                if kind == "event_msg" and payload.get("type") == "thread_settings_applied":
                    _execution_settings(context, payload.get("thread_settings"))
                    continue
                if kind in {"token_usage_record", "compacted"}:
                    response_id = payload.get("response_id" if kind == "token_usage_record" else "compaction_response_id")
                    if not isinstance(response_id, str):
                        continue
                    key = hashlib.sha256(response_id.encode()).hexdigest()
                    pending = context.setdefault("_compactions", {})
                    record = pending.setdefault(key, {})
                    if kind == "compacted":
                        record["matched"] = True
                    else:
                        usage = _usage_counts(payload.get("usage"))
                        if usage and "usage" not in record:
                            try:
                                request_at = datetime.fromisoformat(str(event.get("timestamp")).replace("Z", "+00:00")).isoformat()
                            except ValueError:
                                request_at = None
                            record.update(usage=usage, timestamp=request_at,
                                          turn=context.get("_turn", 0),
                                          settings={k: context[k] for k in ("model", "effort", "service_tier", "reasoning_mode") if k in context},
                                          thread_total=_usage_counts(payload.get("thread_token_usage")))
                            _execution_settings(record["settings"], payload)
                    if replay or key in context.get("_parent_compactions", []):
                        record["covered"] = True
                    continue
                if kind != "event_msg" or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                total_usage = _usage_counts(info.get("total_token_usage"))
                last = _usage_counts(info.get("last_token_usage"))
                total = total_usage["total_tokens"] if total_usage else None
                before = context.get("_totals")
                old_total = previous_total
                duplicate = total is not None and old_total is not None and total == old_total
                if total is not None:
                    previous_total = total
                    context["_totals"] = total_usage
                usage = last
                if usage is None and total_usage:
                    if old_total is None:
                        usage = total_usage
                    elif total > old_total:
                        usage = {key: (total_usage[key] - before[key] if isinstance(before, dict)
                                      and total_usage.get(key) is not None and before.get(key) is not None
                                      and total_usage[key] >= before[key] else None) for key in total_usage}
                        usage["total_tokens"] = total - old_total
                    else:
                        usage = None  # A correction/reset establishes a new baseline.
                if duplicate or replay or usage is None:
                    continue
                fingerprint = hashlib.sha256(json.dumps([event.get("timestamp"), total_usage, usage], sort_keys=True).encode()).hexdigest()
                seen = context.setdefault("_seen_usage", [])
                if fingerprint in seen:
                    continue
                seen.append(fingerprint)
                context["_seen_usage"] = seen[-256:]
                covered_by_compaction = False
                for record in context.get("_compactions", {}).values():
                    if record.get("usage") == usage or (total_usage is not None and record.get("thread_total") == total_usage):
                        record["covered"] = True
                        if record.get("emitted") and record.get("turn") == context.get("_turn", 0):
                            covered_by_compaction = True
                request_settings = {k: context[k] for k in ("model", "effort", "service_tier", "reasoning_mode") if k in context}
                _execution_settings(request_settings, info)
                if not covered_by_compaction:
                    emit(usage, event.get("timestamp"), request_settings)
            next_offset = stream.tell()
            # Pair compaction records in either order, after intervening local snapshots.
            # Do not flush at a byte-budget boundary or an incomplete JSONL line.
            if next_offset == size or (stop_offset is not None and next_offset >= stop_offset):
                pending = context.get("_compactions", {})
                for record in pending.values():
                    if record.get("matched") and record.get("usage") and not record.get("emitted"):
                        if not record.get("covered"):
                            emit(record["usage"], record.get("timestamp"), record.get("settings", {}))
                        record["emitted"] = True
                # Completed IDs stay available across incremental reads for deduplication.
                while len(pending) > 256:
                    removable = next((key for key, record in pending.items() if record.get("emitted")), None)
                    if removable is None:
                        removable = next(iter(pending))
                        context["_evicted_compactions"] = context.get("_evicted_compactions", 0) + 1
                    del pending[removable]
    except OSError:
        return [], offset, initial_total
    return [{"at": at, "tokens": value} for at, value in sorted(buckets.items())[-20_000:]], next_offset, previous_total


def read_sessions(
    state_db: Path, token: str, include_rollout_path: bool = False,
) -> list[dict[str, Any]]:
    if not state_db.is_file():
        raise FileNotFoundError(f"Codex database not found: {state_db}")
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        # rollout_path is used only to read numeric token_count events locally.
        # It is never included in the returned/transmitted payload.
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        rollout_column = "rollout_path" if "rollout_path" in columns else "NULL AS rollout_path"
        rows = connection.execute(
            f"""SELECT id, cwd, model, tokens_used, created_at, updated_at, archived,
                       {rollout_column} FROM threads"""
        ).fetchall()
    finally:
        connection.close()
    sessions = []
    for row in rows:
        sessions.append({
            "key": digest("session", row["id"], token),
            "project_key": digest("project", row["cwd"], token),
            "project_name": Path(row["cwd"]).name or "unknown",
            "model": row["model"] or "unknown",
            "tokens": max(0, int(row["tokens_used"] or 0)),
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
            "archived": bool(row["archived"]),
        })
        if include_rollout_path and row["rollout_path"]:
            sessions[-1]["_rollout_path"] = str(row["rollout_path"])
    return sessions


def find_codex_executable(codex_home: Path, explicit: str | None = None) -> Path:
    """Locate a Codex executable without reading or transmitting user content."""
    configured = explicit or os.getenv("CODEX_EXECUTABLE")
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path
        raise FileNotFoundError("configured Codex executable was not found")

    profile = codex_home.parent
    candidates: list[Path] = []
    for command in ("codex.exe", "codex"):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(Path(resolved))
    candidates.extend((profile / ".local" / "bin").glob("codex"))
    candidates.extend((profile / ".nvm" / "versions" / "node").glob("*/bin/codex"))
    candidates.extend((profile / "AppData" / "Local" / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"))
    for extensions in (profile / ".vscode" / "extensions", profile / ".windsurf" / "extensions"):
        candidates.extend(extensions.glob("openai.chatgpt-*/bin/windows-*/codex.exe"))
        candidates.extend(extensions.glob("openai.chatgpt-*/bin/linux-*/codex"))

    files = [
        candidate for candidate in candidates
        if candidate.is_file() and "\\windowsapps\\" not in str(candidate).casefold()
    ]
    if not files:
        raise FileNotFoundError("Codex executable was not found")
    return max(files, key=lambda path: path.stat().st_mtime)


def _optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _rate_window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        used_percent = min(100.0, max(0.0, float(value.get("usedPercent"))))
    except (TypeError, ValueError):
        return None
    return {
        "used_percent": used_percent,
        "window_minutes": _optional_nonnegative_int(value.get("windowDurationMins")),
        "resets_at": _optional_nonnegative_int(value.get("resetsAt")),
    }


def normalize_app_server_snapshot(
    account_result: dict[str, Any],
    usage_result: dict[str, Any] | None,
    limits_result: dict[str, Any] | None,
    observed_at: int | None = None,
) -> dict[str, Any]:
    """Reduce app-server responses to account metadata and aggregate counters only."""
    account = account_result.get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise RuntimeError("Codex is not signed in with a ChatGPT account")

    raw_summary = usage_result.get("summary") if isinstance(usage_result, dict) else {}
    raw_summary = raw_summary if isinstance(raw_summary, dict) else {}
    summary = {
        "lifetime_tokens": _optional_nonnegative_int(raw_summary.get("lifetimeTokens")),
        "peak_daily_tokens": _optional_nonnegative_int(raw_summary.get("peakDailyTokens")),
        "longest_running_turn_seconds": _optional_nonnegative_int(raw_summary.get("longestRunningTurnSec")),
        "current_streak_days": _optional_nonnegative_int(raw_summary.get("currentStreakDays")),
        "longest_streak_days": _optional_nonnegative_int(raw_summary.get("longestStreakDays")),
    }

    daily_usage = []
    raw_daily = usage_result.get("dailyUsageBuckets") if isinstance(usage_result, dict) else []
    if isinstance(raw_daily, list):
        for bucket in raw_daily[-400:]:
            date = str(bucket.get("startDate") or "") if isinstance(bucket, dict) else ""
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                continue
            tokens = _optional_nonnegative_int(bucket.get("tokens"))
            if tokens is not None:
                daily_usage.append({"date": date, "tokens": tokens})
    daily_usage.sort(key=lambda bucket: bucket["date"])

    rate_limits = []
    if isinstance(limits_result, dict):
        by_id = limits_result.get("rateLimitsByLimitId")
        if isinstance(by_id, dict) and by_id:
            raw_limits = list(by_id.items())
        else:
            single = limits_result.get("rateLimits")
            raw_limits = [((single or {}).get("limitId") or "codex", single)] if isinstance(single, dict) else []
        for limit_id, limit in raw_limits[:16]:
            if not isinstance(limit, dict):
                continue
            primary = _rate_window(limit.get("primary"))
            secondary = _rate_window(limit.get("secondary"))
            if primary is None and secondary is None:
                continue
            rate_limits.append({
                "id": str(limit_id)[:80],
                "name": str(limit.get("limitName") or limit_id)[:120],
                "plan": str(limit.get("planType"))[:80] if limit.get("planType") else None,
                "primary": primary,
                "secondary": secondary,
            })

    return {
        "account": {
            "type": "chatgpt",
            "email": str(account.get("email"))[:320] if account.get("email") else None,
            "plan": str(account.get("planType"))[:80] if account.get("planType") else None,
        },
        "usage": {
            "source": "codex-app-server",
            "observed_at": int(observed_at or time.time()),
            "summary": summary,
            "daily_usage": daily_usage,
            "rate_limits": rate_limits,
        },
    }


def read_app_server_usage(
    codex_home: Path,
    executable: str | None = None,
    timeout: int = APP_SERVER_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Query aggregate account usage over the local Codex app-server JSONL protocol."""
    codex = find_codex_executable(codex_home, executable)
    auth_file = codex_home / "auth.json"
    if not auth_file.is_file():
        raise FileNotFoundError("Codex auth file was not found")
    query_directory = tempfile.TemporaryDirectory(prefix="codex-usage-query-")
    query_home = Path(query_directory.name)
    shutil.copy2(auth_file, query_home / "auth.json")
    if (codex_home / "installation_id").is_file():
        shutil.copy2(codex_home / "installation_id", query_home / "installation_id")
    environment = os.environ.copy()
    profile = codex_home.parent.resolve()
    executable_directory = str(codex.parent.resolve())
    environment.update({
        # Account endpoints need auth state but not session content. An isolated,
        # writable CODEX_HOME also avoids modifying or locking the live profile.
        "CODEX_HOME": str(query_home),
        "HOME": str(profile),
        "USERPROFILE": str(profile),
        "LOCALAPPDATA": str(profile / "AppData" / "Local"),
        "APPDATA": str(profile / "AppData" / "Roaming"),
        # npm/NVM installs expose Codex through a /usr/bin/env node launcher.
        # Scheduled services often have a minimal PATH, so include its sibling runtime.
        "PATH": executable_directory + os.pathsep + environment.get("PATH", ""),
    })
    try:
        process = subprocess.Popen(
            [str(codex), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        query_directory.cleanup()
        raise
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise RuntimeError("could not open Codex app-server pipes")

    messages = [
        {"method": "initialize", "id": 1, "params": {
            "clientInfo": {
                "name": "codex_usage_monitor",
                "title": "Codex Usage Monitor",
                "version": COLLECTOR_VERSION,
            },
            "capabilities": {"experimentalApi": True},
        }},
        {"method": "initialized", "params": {}},
        {"method": "account/read", "id": 2, "params": {"refreshToken": False}},
        {"method": "account/usage/read", "id": 3, "params": {}},
        {"method": "account/rateLimits/read", "id": 4, "params": {}},
    ]
    responses: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        try:
            for line in process.stdout:
                responses.put(line)
        finally:
            responses.put(None)

    threading.Thread(target=read_output, daemon=True, name="codex-app-server-reader").start()
    try:
        for message in messages:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()
        pending = {1, 2, 3, 4}
        results: dict[int, dict[str, Any] | None] = {}
        deadline = time.monotonic() + max(2, timeout)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Codex app-server query timed out")
            try:
                line = responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise RuntimeError("Codex app-server query timed out") from exc
            if line is None:
                raise RuntimeError("Codex app-server closed before responding")
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            response_id = response.get("id")
            if response_id not in pending:
                continue
            pending.remove(response_id)
            if response.get("error") is not None:
                results[response_id] = None
            else:
                result = response.get("result")
                results[response_id] = result if isinstance(result, dict) else {}
        if results.get(1) is None or results.get(2) is None:
            raise RuntimeError("Codex app-server initialization or account query failed")
        if results.get(3) is None and results.get(4) is None:
            raise RuntimeError("Codex account usage and rate-limit queries failed")
        return normalize_app_server_snapshot(results[2] or {}, results.get(3), results.get(4))
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        query_directory.cleanup()


def attach_account_usage(payload: dict[str, Any], codex_home: Path) -> None:
    live = read_app_server_usage(codex_home)
    local_email = str(payload.get("account", {}).get("email") or "").casefold()
    live_email = str(live.get("account", {}).get("email") or "").casefold()
    if local_email and live_email and local_email != live_email:
        raise RuntimeError("Codex app-server account does not match the local auth profile")
    if live_email:
        payload["account"]["email"] = live["account"]["email"]
    if live.get("account", {}).get("plan"):
        payload["account"]["plan"] = live["account"]["plan"]
    payload["account_usage"] = live["usage"]


def _merge_usage_detail(target: dict[str, Any], detail: dict[str, Any]) -> None:
    key = "|".join(str(detail[field]) for field in ("at", "model", "effort", "service_tier", "reasoning_mode"))
    previous = target.get(key)
    if previous is None:
        target[key] = dict(detail)
        return
    combined = dict(detail)
    for field in ("tokens", "events", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
        before, after = previous.get(field), detail.get(field)
        combined[field] = before + after if before is not None and after is not None else None
    target[key] = combined


def track_target_usage(
    sessions: list[dict[str, Any]],
    current_account_key: str,
    target_account_key: str,
    auth_file: Path,
    state_file: Path,
    scope_key: str,
    allow_initial: bool = True,
    accumulate: bool = True,
) -> list[dict[str, Any]]:
    """Accumulate token growth observed while the target account is logged in."""
    try:
        state = json.loads(state_file.read_text())
        if not isinstance(state, dict) or not isinstance(state.get("profiles"), dict):
            raise ValueError("invalid attribution state")
    except FileNotFoundError:
        state = {"version": 2, "profiles": {}}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read collector attribution state: {exc}") from exc

    scan_at = int(time.time())
    profiles = state["profiles"]
    first_scan = scope_key not in profiles
    profile_state = profiles.setdefault(scope_key, {"sessions": {}})
    records = profile_state.get("sessions")
    if not isinstance(records, dict):
        raise RuntimeError("invalid collector usage profile")

    try:
        login_transition_at = int(auth_file.stat().st_mtime)
    except OSError:
        login_transition_at = int(time.time())
    previous_account_key = profile_state.get("last_account_key")
    account_changed = bool(previous_account_key and previous_account_key != current_account_key)
    previous_scan_at = int(profile_state.get("last_scan_at") or scan_at)

    intervals = profile_state.get("account_intervals")
    if not isinstance(intervals, list):
        intervals = []
    intervals = [
        item for item in intervals
        if isinstance(item, dict) and int(item.get("end") or 0) >= scan_at - 14 * 86400
    ]
    continuous = not first_scan and 0 <= scan_at - previous_scan_at <= 120
    if continuous and previous_account_key == current_account_key:
        if intervals and intervals[-1].get("account_key") == current_account_key:
            intervals[-1]["end"] = scan_at
        else:
            intervals.append({"start": previous_scan_at, "end": scan_at, "account_key": current_account_key})
    elif continuous and previous_account_key:
        transition = min(scan_at, max(previous_scan_at, login_transition_at))
        if intervals and intervals[-1].get("account_key") == previous_account_key:
            intervals[-1]["end"] = transition
        else:
            intervals.append({"start": previous_scan_at, "end": transition, "account_key": previous_account_key})
        intervals.append({"start": transition, "end": scan_at, "account_key": current_account_key})
    else:
        # Do not assume the account stayed unchanged across first install,
        # sleep, service downtime, or another long observation gap.
        intervals.append({"start": scan_at, "end": scan_at, "account_key": current_account_key})

    backfill_budget = 32 * 1024 * 1024
    # Recent requests must not wait behind old multi-gigabyte session files.
    sessions = sorted(sessions, key=lambda session: int(session.get("updated_at") or 0), reverse=True)
    for session in sessions:
        session_key = str(session["key"])
        current_tokens = max(0, int(session.get("tokens") or 0))
        record = records.get(session_key)
        existing_full_ownership = (
            isinstance(record, dict) and current_tokens > 0
            and bool(session.get("_rollout_path"))
            and int(record.get("target_tokens") or 0) > 0
            and int(record.get("target_tokens") or 0) == int(record.get("last_tokens") or 0)
            and int(record.get("last_tokens") or 0) <= current_tokens
            and previous_account_key == current_account_key == target_account_key
            and int(session.get("created_at") or 0) >= login_transition_at - 5
        )
        if isinstance(record, str):
            # Migrate the v0.4.3 first-account binding conservatively.
            record = {
                "last_tokens": current_tokens,
                "target_tokens": current_tokens if record == target_account_key else 0,
            }
        elif isinstance(record, dict):
            record = dict(record)
            previous_tokens = max(0, int(record.get("last_tokens") or 0))
            target_tokens = max(0, int(record.get("target_tokens") or 0))
            delta_account_key = current_account_key
            if account_changed and int(session.get("updated_at") or 0) <= login_transition_at + 5:
                delta_account_key = str(previous_account_key)
            if accumulate and continuous and current_tokens >= previous_tokens and delta_account_key == target_account_key:
                target_tokens += current_tokens - previous_tokens
            record["last_tokens"] = current_tokens
            record["target_tokens"] = current_tokens if existing_full_ownership and accumulate else target_tokens
        else:
            can_claim_initial = (
                accumulate and current_account_key == target_account_key
                and (
                    (allow_initial and first_scan and int(session["created_at"]) >= login_transition_at - 5)
                    or (continuous and int(session["created_at"]) >= max(
                        previous_scan_at, login_transition_at if account_changed else previous_scan_at,
                    ))
                )
            )
            record = {
                "last_tokens": current_tokens,
                "target_tokens": current_tokens if can_claim_initial else 0,
            }
        rollout_path = session.get("_rollout_path")
        pending = record.get("usage_buckets")
        pending = dict(pending) if isinstance(pending, dict) else {}
        cutoff = scan_at - 14 * 86400
        pending = {
            str(at): int(tokens)
            for at, tokens in pending.items()
            if str(at).isdigit() and int(at) >= cutoff and int(tokens) >= 0
        }
        candidates = record.get("usage_candidates")
        candidates = dict(candidates) if isinstance(candidates, dict) else {}
        candidates = {
            str(at): int(tokens)
            for at, tokens in candidates.items()
            if str(at).isdigit() and int(at) >= cutoff and int(tokens) >= 0
        }
        detail_pending = {key: value for key, value in (record.get("usage_details") or {}).items()
                          if isinstance(value, dict) and int(value.get("at") or 0) >= cutoff}
        detail_candidates = {key: value for key, value in (record.get("detail_candidates") or {}).items()
                             if isinstance(value, dict) and int(value.get("at") or 0) >= cutoff}
        if rollout_path and record.get("usage_parser_version") != 3:
            # Rebuild corrected details separately. The server keeps old data
            # until a complete replacement has been received atomically.
            detail_pending, detail_candidates = {}, {}
            for key in ("usage_details_version", "detail_backfill_end", "detail_backfill_offset",
                        "detail_backfill_total", "detail_backfill_context"):
                record.pop(key, None)
            record["usage_parser_version"] = 3
        if rollout_path:
            path = Path(str(rollout_path))
            previous_offset = record.get("rollout_offset")
            if previous_offset is None:
                try:
                    record["rollout_offset"] = path.stat().st_size
                except OSError:
                    record["rollout_offset"] = 0
                previous_offset = record["rollout_offset"]
            else:
                new_details = []
                context = record.get("rollout_context")
                context = dict(context) if isinstance(context, dict) else {}
                new_buckets, next_offset, last_total = _read_rollout_usage_buckets(
                    rollout_path, cutoff, int(previous_offset), record.get("rollout_total_tokens"),
                    new_details, context,
                )
                record["rollout_offset"] = next_offset
                record["rollout_total_tokens"] = last_total
                record["rollout_context"] = context
                for detail in new_details:
                    _merge_usage_detail(detail_candidates, detail)
                for bucket in new_buckets:
                    key = str(int(bucket["at"]))
                    candidates[key] = candidates.get(key, 0) + int(bucket["tokens"])
            if record.get("usage_details_version") != 3 and path.is_file() and backfill_budget > 0:
                # Existing collectors know the account's historical continuity
                # intervals. Re-read numeric usage plus turn execution metadata
                # only; legacy total-token buckets must not be added again.
                backfill_end = record.setdefault("detail_backfill_end", int(previous_offset))
                backfill_offset = int(record.get("detail_backfill_offset") or 0)
                backfill_context = record.get("detail_backfill_context") or {}
                recovered = []
                _, recovered_offset, recovered_total = _read_rollout_usage_buckets(
                    rollout_path, cutoff, backfill_offset, record.get("detail_backfill_total"),
                    recovered, backfill_context, int(backfill_end), min(backfill_budget, 8 * 1024 * 1024),
                )
                backfill_budget -= max(0, recovered_offset - backfill_offset)
                record["detail_backfill_offset"] = recovered_offset
                record["detail_backfill_total"] = recovered_total
                record["detail_backfill_context"] = backfill_context
                for detail in recovered:
                    _merge_usage_detail(detail_candidates, detail)
                if recovered_offset >= int(backfill_end):
                    record["usage_details_version"] = 3
                    if not (record.get("rollout_context") or {}).get("model"):
                        record["rollout_context"] = dict(backfill_context)
        remaining_candidates: dict[str, int] = {}
        for key, tokens in candidates.items():
            occurred_at = int(key)
            interval = next(
                (
                    item for item in reversed(intervals)
                    if int(item.get("start") or 0) <= occurred_at
                    and occurred_at + 60 <= int(item.get("end") or 0)
                ),
                None,
            )
            if interval is not None:
                if accumulate and interval.get("account_key") == target_account_key:
                    pending[key] = pending.get(key, 0) + tokens
            elif occurred_at + 60 > scan_at:
                # The current minute cannot be attributed until a later scan
                # proves that the same account remained active for all of it.
                remaining_candidates[key] = tokens
        record["usage_buckets"] = pending
        record["usage_candidates"] = remaining_candidates
        remaining_details = {}
        for key, detail in detail_candidates.items():
            occurred_at = detail["at"]
            interval = next((item for item in reversed(intervals)
                             if int(item.get("start") or 0) <= occurred_at
                             and occurred_at + 60 <= int(item.get("end") or 0)), None)
            if interval is not None:
                if accumulate and interval.get("account_key") == target_account_key:
                    _merge_usage_detail(detail_pending, detail)
            elif accumulate and existing_full_ownership and occurred_at + 60 > int(session.get("created_at") or 0):
                # A pre-existing ledger assigning the ENTIRE session to this
                # account can recover its request breakdown across a polling
                # gap. A fresh login, split ledger or first scan cannot.
                _merge_usage_detail(detail_pending, detail)
            elif occurred_at + 60 > scan_at:
                remaining_details[key] = detail
        record["usage_details"] = detail_pending
        record["detail_candidates"] = remaining_details
        records[session_key] = record

    state["version"] = 2
    profile_state["last_account_key"] = current_account_key
    profile_state["last_scan_at"] = scan_at
    profile_state["account_intervals"] = intervals[-1000:]
    profile_state["target_account_key"] = target_account_key

    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_name(state_file.name + ".tmp")
    try:
        temporary.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(state_file)
    except OSError as exc:
        raise RuntimeError(f"could not save collector attribution state: {exc}") from exc

    tracked = []
    bucket_budget = 10_000
    detail_budget = 10_000
    for session in sessions:
        target_tokens = int(records[str(session["key"])]["target_tokens"])
        if target_tokens <= 0:
            continue
        attributed = {
            key: value for key, value in session.items()
            if key not in ("_rollout_path", "_usage_buckets")
        }
        attributed["tokens"] = target_tokens
        if session.get("_rollout_path"):
            attributed["usage_detail_schema"] = 2
            attributed["usage_details_since"] = max(0, ((scan_at - 14 * 86400 + 59) // 60) * 60)
        attributed["usage_detail_backfill_complete"] = records[str(session["key"])].get("usage_details_version") == 3
        verified_buckets = [
            {"at": int(at), "tokens": int(tokens)}
            for at, tokens in sorted(
                records[str(session["key"])].get("usage_buckets", {}).items(),
                key=lambda item: int(item[0]),
            )
        ]
        if verified_buckets and bucket_budget > 0:
            selected_buckets = verified_buckets[-bucket_budget:]
            attributed["usage_buckets"] = selected_buckets
            bucket_budget -= len(selected_buckets)
        if detail_budget > 0:
            details = sorted(records[str(session["key"])].get("usage_details", {}).values(), key=lambda item: item["at"])
            if details:
                attributed["usage_details"] = details[-detail_budget:]
                attributed["usage_detail_backfill_complete"] &= len(details) <= detail_budget
                detail_budget -= len(attributed["usage_details"])
        else:
            attributed["usage_detail_backfill_complete"] = False
        tracked.append(attributed)
    return tracked


def append_log(path: Path, line: str, max_bytes: int, backup_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded_size = len((line + "\n").encode("utf-8"))
    if max_bytes > 0 and path.is_file() and path.stat().st_size + encoded_size > max_bytes:
        if backup_count <= 0:
            path.unlink()
        else:
            oldest = path.with_name(f"{path.name}.{backup_count}")
            if oldest.exists():
                oldest.unlink()
            for index in range(backup_count - 1, 0, -1):
                source = path.with_name(f"{path.name}.{index}")
                if source.exists():
                    source.replace(path.with_name(f"{path.name}.{index + 1}"))
            path.replace(path.with_name(f"{path.name}.1"))
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def snapshot(
    state_db: Path, auth_file: Path, token: str, include_rollout_path: bool = False,
) -> dict[str, Any]:
    hostname, os_user = socket.gethostname(), getpass.getuser()
    return {
        "version": 1,
        "collector": {"version": COLLECTOR_VERSION, "kind": "user", "observed_at": int(time.time())},
        "node": {
            "key": hashlib.sha256(f"{hostname}:{os_user}".encode()).hexdigest()[:12],
            "hostname": hostname,
            "os_user": os_user,
        },
        "account": read_account(auth_file, token),
        "sessions": (
            read_sessions(state_db, token, include_rollout_path=include_rollout_path)
            if state_db.is_file() else []
        ),
    }


def send(server: str, payload: dict[str, Any], token: str, path: str = "/api/ingest") -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(token.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    endpoint = server.rstrip("/") + path
    req = request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Codex-Timestamp": timestamp,
            "X-Codex-Signature": signature,
        },
    )
    with request.urlopen(req, timeout=15) as response:
        if response.status not in (200, 202):
            raise RuntimeError(f"collector rejected: HTTP {response.status}")
        response_body = response.read()
        response_signature = response.headers.get("X-Codex-Response-Signature", "")
    expected_response = hmac.new(token.encode(), response_body, hashlib.sha256).hexdigest()
    if not response_signature or not hmac.compare_digest(response_signature, expected_response):
        raise RuntimeError("collector response signature is invalid")
    try:
        decoded = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("collector response is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("collector response is invalid")
    return decoded


def collector_configuration(args):
    """Reload persisted settings, including for old tasks with fixed arguments."""
    path = args.config_file or args.token_file.with_name("collector-config.json")
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(settings, dict):
            raise ValueError("configuration must be an object")
    except FileNotFoundError:
        settings = {}
    server = settings.get("server", args.server)
    if not isinstance(server, str) or any(ord(char) < 32 or ord(char) == 127 for char in server):
        raise ValueError("invalid server URL")
    parsed = urlparse(server or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("server must be an HTTP(S) URL without credentials, query, or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("invalid server port") from exc
    account_key = settings.get("account_key") or args.account_key
    if isinstance(account_key, str):
        account_key = account_key.lower()
    if account_key is not None and (not isinstance(account_key, str) or not re.fullmatch(r"[a-f0-9]{12}", account_key)):
        raise ValueError("invalid legacy account key")
    return path, {"server": server.rstrip("/"), "token_file": str(Path(settings.get("token_file", args.token_file)).expanduser()),
                  "account_key": account_key}


def configure_collector(args):
    path = args.config_file or args.token_file.with_name("collector-config.json")
    if args.configure:
        try:
            previous = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            previous = {}
        if not isinstance(previous, dict):
            raise ValueError("configuration must be an object")
        # Only explicitly supplied options replace existing settings. Merely
        # changing the URL must not replace the installed token or account.
        for flag, field, value in (("--server", "server", args.server), ("--token-file", "token_file", str(args.token_file)),
                                   ("--account-key", "account_key", args.account_key)):
            if flag in sys.argv or any(argument.startswith(flag + "=") for argument in sys.argv):
                previous[field] = value
        if not previous.get("server") and args.server:
            previous["server"] = args.server
        # Validate before replacing the configuration.
        original = path
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "settings.json"
            candidate.write_text(json.dumps(previous))
            args.config_file = candidate
            _, settings = collector_configuration(args)
        args.config_file = original
        token_path = Path(settings["token_file"])
        if not token_path.is_file() or not token_path.read_text().strip():
            raise ValueError("collector token file is missing or empty")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        print(f"Saved {path}; the running collector reloads it on its next scan.")
    if args.show_config:
        _, settings = collector_configuration(args)
        print(json.dumps(settings, indent=2))
    return args.configure or args.show_config


def registered_snapshots(payload, configuration, auth_file, state_file, token, legacy_target=None):
    """Observe all registered targets, retaining a separate ledger for each."""
    base = attribution_scope(payload["node"]["key"], token)
    try:
        state = json.loads(state_file.read_text())
    except FileNotFoundError:
        state = {}
    profile = state.get("profiles", {}).get(base, {})
    original_target = profile.get("target_account_key") or legacy_target or payload["account"].get("key")
    targets = set(configuration["tracked_accounts"])
    if profile and original_target and original_target != "unknown":
        targets.add(original_target)
    enabled = set(configuration["monitored_accounts"])
    result = []
    for key in sorted(targets):
        scope = base if key == original_target else f"{base}:{key}"
        sessions = track_target_usage(payload["sessions"], payload["account"].get("key") or "unknown", key,
                                      auth_file, state_file, scope, allow_initial=key == original_target,
                                      accumulate=key in enabled)
        if key in enabled:
            account = payload["account"] if key == payload["account"].get("key") else {"key": key}
            result.append(dict(payload, account=account, sessions=sessions))
    return result


def monitoring_configuration(server, payload, token, legacy_target):
    discovery = {"account": payload["account"], "node": payload["node"], "collector": payload.get("collector")}
    try:
        result = send(server, discovery, token, path="/api/collector/config")
    except error.HTTPError as exc:
        if exc.code != 404:
            raise
        target = legacy_target or payload["account"].get("key")
        result = {"tracked_accounts": [target], "monitored_accounts": [target]}
    for field in ("tracked_accounts", "monitored_accounts"):
        if not isinstance(result.get(field), list) or any(not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{12}", key) for key in result[field]):
            raise RuntimeError("invalid server account configuration")
    if not set(result["monitored_accounts"]).issubset(result["tracked_accounts"]):
        raise RuntimeError("invalid enabled account configuration")
    return result


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return ()


def apply_collector_update(update: dict[str, Any], script_dir: Path | None = None) -> bool:
    """Atomically install a server-signed update already verified by send()."""
    version = str(update.get("version") or "")
    if not _version_tuple(version) or _version_tuple(version) <= _version_tuple(COLLECTOR_VERSION):
        return False
    raw_files = update.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise RuntimeError("collector update has no files")
    target_dir = (script_dir or Path(__file__).resolve().parent).resolve()
    prepared: list[tuple[Path, Path, Path | None]] = []
    replaced: list[tuple[Path, Path | None]] = []
    try:
        for name, metadata in raw_files.items():
            if name not in UPDATE_FILE_NAMES or not isinstance(metadata, dict):
                raise RuntimeError("collector update contains an invalid file")
            try:
                content = base64.b64decode(str(metadata.get("content") or ""), validate=True)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("collector update content is invalid") from exc
            checksum = hashlib.sha256(content).hexdigest()
            if not hmac.compare_digest(checksum, str(metadata.get("sha256") or "")):
                raise RuntimeError("collector update checksum mismatch")
            compile(content, name, "exec")
            target = target_dir / name
            temporary = target.with_name(target.name + ".update")
            backup = target.with_name(target.name + ".previous") if target.exists() else None
            temporary.write_bytes(content)
            try:
                temporary.chmod(target.stat().st_mode & 0o777 if target.exists() else 0o755)
            except OSError:
                pass
            prepared.append((target, temporary, backup))
        for target, temporary, backup in prepared:
            if backup is not None:
                if backup.exists():
                    backup.unlink()
                target.replace(backup)
            replaced.append((target, backup))
            temporary.replace(target)
        if os.name == "nt":
            try:
                import winreg
                for key_path in (
                    r"Software\CodexUsageCollector",
                    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector",
                ):
                    with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE,
                    ) as registry_key:
                        winreg.SetValueEx(registry_key, "DisplayVersion", 0, winreg.REG_SZ, version)
            except (ImportError, OSError):
                pass
        return True
    except Exception:
        for target, temporary, _ in reversed(prepared):
            if temporary.exists():
                temporary.unlink()
        for target, backup in reversed(replaced):
            if backup is not None and backup.exists():
                if target.exists():
                    target.unlink()
                backup.replace(target)
            elif backup is None and target.exists():
                target.unlink()
        raise


def show_notification(alert: dict[str, Any], windows_user: str | None = None) -> bool:
    title = str(alert.get("title") or "Codex Usage Monitor")[:120]
    message = str(alert.get("message") or "")[:800]
    if not message:
        return False
    try:
        if os.name == "nt":
            recipient = windows_user or getpass.getuser()
            subprocess.run(
                ["msg.exe", recipient, "/TIME:60", f"{title}\n{message}"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10,
            )
            return True
        executable = shutil.which("notify-send")
        if executable:
            subprocess.run(
                [executable, title, message], check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=10,
            )
            return True
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def read_alert_receipts(state_file: Path) -> list[str]:
    receipt_file = state_file.with_name("collector-alerts.json")
    try:
        receipts = json.loads(receipt_file.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(receipts, list):
        return []
    return [str(item) for item in receipts[-200:] if isinstance(item, str)]


def process_server_response(
    response: dict[str, Any], state_file: Path, log: Any,
    windows_user: str | None = None, script_dir: Path | None = None,
) -> None:
    alerts = response.get("alerts")
    if isinstance(alerts, list):
        receipt_file = state_file.with_name("collector-alerts.json")
        try:
            receipts = read_alert_receipts(state_file)
        except (OSError, json.JSONDecodeError):
            receipts = []
        known = {str(item) for item in receipts}
        for alert in alerts:
            if not isinstance(alert, dict) or str(alert.get("id") or "") in known:
                continue
            shown = show_notification(alert, windows_user)
            log(f"alert id={alert.get('id')} desktop={'shown' if shown else 'unavailable'} message={alert.get('message')}")
            known.add(str(alert.get("id")))
        try:
            receipt_file.write_text(json.dumps(sorted(known)[-200:], separators=(",", ":")))
            os.chmod(receipt_file, 0o600)
        except OSError:
            pass
    update = response.get("update")
    if isinstance(update, dict) and apply_collector_update(update, script_dir):
        log(f"installed collector update version={update.get('version')}; restarting")
        raise CollectorRestart("collector update installed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=os.getenv("CODEX_MONITOR_SERVER"))
    parser.add_argument("--token-file", type=Path, default=Path(os.getenv("CODEX_COLLECTOR_TOKEN_FILE", "~/.config/codex-usage-collector.token")).expanduser())
    parser.add_argument("--state-db", type=Path, default=Path("~/.codex/state_5.sqlite").expanduser())
    parser.add_argument("--auth-file", type=Path, default=Path("~/.codex/auth.json").expanduser())
    parser.add_argument("--account-key", default=os.getenv("CODEX_TARGET_ACCOUNT_KEY"), help="Legacy account binding for attribution migration; monitored targets are managed by the server")
    parser.add_argument("--state-file", type=Path, default=Path(os.getenv("CODEX_COLLECTOR_STATE_FILE", "~/.config/codex-usage-collector-state.json")).expanduser())
    parser.add_argument("--interval", type=int, default=int(os.getenv("CODEX_COLLECTOR_INTERVAL", "30")))
    parser.add_argument("--log-file", type=Path, help="Optional log file (useful for Windows scheduled tasks)")
    parser.add_argument("--log-max-bytes", type=int, default=int(os.getenv("CODEX_COLLECTOR_LOG_MAX_BYTES", str(DEFAULT_LOG_MAX_BYTES))))
    parser.add_argument("--log-backups", type=int, default=int(os.getenv("CODEX_COLLECTOR_LOG_BACKUPS", str(DEFAULT_LOG_BACKUPS))))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--config-file", type=Path, default=Path(os.environ["CODEX_COLLECTOR_CONFIG_FILE"]).expanduser() if os.getenv("CODEX_COLLECTOR_CONFIG_FILE") else None)
    parser.add_argument("--configure", action="store_true", help="Save connection settings and exit")
    parser.add_argument("--show-config", action="store_true", help="Show resolved settings without token contents")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        if configure_collector(args):
            return
        collector_configuration(args)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    def log(message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        if sys.stdout:
            print(line, flush=True)
        if args.log_file:
            append_log(args.log_file, line, max(0, args.log_max_bytes), max(0, args.log_backups))

    while True:
        try:
            _, settings = collector_configuration(args)
            token = Path(settings["token_file"]).read_text().strip()
            if not token:
                raise RuntimeError("collector token is empty")
            payload = snapshot(args.state_db, args.auth_file, token, include_rollout_path=True)
            payload["alert_receipts"] = read_alert_receipts(args.state_file)
            configuration = monitoring_configuration(settings["server"], payload, token, settings["account_key"])
            process_server_response(configuration, args.state_file, log)
            reports = registered_snapshots(payload, configuration, args.auth_file, args.state_file, token, settings["account_key"])
            for report in reports:
                usage_source = "local-fallback"
                if report["account"]["key"] == payload["account"].get("key"):
                    try:
                        attach_account_usage(report, args.auth_file.parent)
                        usage_source = "codex-app-server"
                    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                        log(f"account usage fallback: {exc}")
                response = send(settings["server"], report, token)
                log(f"sent account={report['account']['key']} sessions={len(report['sessions'])} source={usage_source} from {payload['node']['hostname']}:{payload['node']['os_user']}")
                process_server_response(response, args.state_file, log)
            if not reports:
                log("account discovered; waiting for the server administrator to enable monitoring")
        except CollectorRestart:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except (OSError, ValueError, sqlite3.Error, error.URLError, RuntimeError) as exc:
            log(f"collector error: {exc}")
            if args.once:
                raise SystemExit(1) from exc
        if args.once:
            return
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    main()
