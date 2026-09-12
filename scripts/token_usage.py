#!/usr/bin/env python3
"""Report Codex token usage for orchestrated sessions.

Codex writes one rollout JSONL file per thread under $CODEX_HOME/sessions
(default ~/.codex/sessions). Subagent threads share the root thread's
`session_id`, so grouping rollouts by it gives the cost of one task,
split by thread, role, and model.

  scripts/token_usage.py sessions [--date YYYY-MM-DD] [--limit N]
  scripts/token_usage.py report [SESSION_ID_PREFIX] [--date YYYY-MM-DD] [--format md|json|csv]

`report` without an id picks the newest session that spawned subagents.
Read-only and standard library only.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
AUTO_REVIEW_ROLES = {"guardian", "guardian_review"}
EPOCH = datetime.min.replace(tzinfo=timezone.utc)
# Cheap pre-filter: rollouts are dominated by large response items we never need to parse.
INTERESTING = ('"turn_context"', '"token_usage_record"', '"token_count"')


def parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def default_sessions_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def rollout_paths(sessions_dir: Path, date: str | None):
    if date:
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise SystemExit(f"--date must be YYYY-MM-DD, got {date!r}")
        base = sessions_dir / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        return sorted(base.glob("rollout-*.jsonl")) if base.is_dir() else []
    return sorted(sessions_dir.rglob("rollout-*.jsonl"))


def read_meta(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            record = json.loads(fh.readline())
    except (OSError, ValueError):
        return None
    if record.get("type") != "session_meta":
        return None
    return record.get("payload") or {}


def role_of(meta: dict) -> tuple[str, str]:
    """Return (role, nickname) for a thread's session_meta payload."""
    source = meta.get("source")
    if isinstance(source, dict):
        sub = source.get("subagent")
        if isinstance(sub, str):
            return sub, ""
        if isinstance(sub, dict):
            spawn = sub.get("thread_spawn")
            if isinstance(spawn, dict):
                return spawn.get("agent_role") or "subagent", spawn.get("agent_nickname") or ""
            if sub.get("other"):
                return str(sub["other"]), ""
    if meta.get("id") and meta.get("id") == meta.get("session_id"):
        return "root", ""
    return meta.get("thread_source") or "unknown", ""


def group_sessions(sessions_dir: Path, date: str | None) -> dict[str, list[tuple[Path, dict]]]:
    groups: dict[str, list[tuple[Path, dict]]] = defaultdict(list)
    for path in rollout_paths(sessions_dir, date):
        meta = read_meta(path)
        if meta is not None:
            groups[meta.get("session_id") or meta.get("id") or path.stem].append((path, meta))
    return groups


def analyze(path: Path, meta: dict) -> dict:
    role, nickname = role_of(meta)
    usage: dict[str, Counter] = defaultdict(Counter)
    responses: Counter = Counter()
    model = effort = None
    started = ended = parse_time(meta.get("timestamp"))
    cumulative = None
    limits_first = limits_last = None

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not any(marker in line for marker in INTERESTING):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            stamp = parse_time(record.get("timestamp"))
            if stamp and (ended is None or stamp > ended):
                ended = stamp
            kind = record.get("type")
            payload = record.get("payload") or {}
            if kind == "event_msg":
                kind = payload.get("type")
            if kind == "turn_context":
                model = payload.get("model") or model
                effort = payload.get("effort") or payload.get("reasoning_effort") or effort
            elif kind == "token_usage_record":
                key = model or "unknown"
                for field in FIELDS:
                    usage[key][field] += int((payload.get("usage") or {}).get(field) or 0)
                responses[key] += 1
            elif kind == "token_count":
                info = payload.get("info") or {}
                cumulative = info.get("total_token_usage") or cumulative
                if payload.get("rate_limits"):
                    limits_first = limits_first or payload["rate_limits"]
                    limits_last = payload["rate_limits"]

    if not usage and cumulative:  # older Codex builds without per-response records
        for field in FIELDS:
            usage[model or "unknown"][field] = int(cumulative.get(field) or 0)

    return {
        "id": meta.get("id") or path.stem,
        "parent_thread_id": meta.get("parent_thread_id"),
        "role": role,
        "nickname": nickname,
        "model": model,
        "effort": effort,
        "cwd": meta.get("cwd"),
        "cli_version": meta.get("cli_version"),
        "started": started,
        "ended": ended,
        "usage": {m: dict(c) for m, c in usage.items()},
        "responses": dict(responses),
        "rate_limits_first": limits_first,
        "rate_limits_last": limits_last,
        "counted": role not in AUTO_REVIEW_ROLES,
        "path": str(path),
    }


def subagent_count(session_id: str, items: list[tuple[Path, dict]]) -> int:
    return sum(
        1 for _, meta in items if meta.get("id") != session_id and role_of(meta)[0] not in AUTO_REVIEW_ROLES
    )


def session_start(session_id: str, items: list[tuple[Path, dict]]) -> datetime:
    root = next((m for _, m in items if m.get("id") == session_id), items[0][1])
    return parse_time(root.get("timestamp")) or EPOCH


# --------------------------------------------------------------------------- output


def n(value: int) -> str:
    return f"{value:,}"


def duration(start: datetime | None, end: datetime | None) -> str:
    if not start or not end:
        return "-"
    seconds = max(0, int((end - start).total_seconds()))
    return f"{seconds // 60}m{seconds % 60:02d}s"


def window(limits: dict | None, key: str) -> str:
    part = (limits or {}).get(key) or {}
    return f"{part['used_percent']}%" if "used_percent" in part else "-"


def rows(threads: list[dict], include_auto_review: bool):
    for thread in threads:
        if not (thread["counted"] or include_auto_review):
            continue
        for model, usage in sorted(thread["usage"].items()):
            yield thread, model, usage


def render_md(session_id: str, threads: list[dict], include_auto_review: bool) -> str:
    root = next((t for t in threads if t["role"] == "root"), threads[0])
    counted = [t for t in threads if t["counted"] or include_auto_review]
    starts = [t["started"] for t in threads if t["started"]]
    ends = [t["ended"] for t in threads if t["ended"]]
    out = [
        f"### Session `{session_id[:8]}`",
        "",
        f"- cwd: `{root['cwd']}`",
        f"- codex: `{root['cli_version']}`",
        f"- threads: {len(threads)} ({len(counted)} counted)",
        f"- wall time: {duration(min(starts), max(ends)) if starts else '-'}",
    ]
    limited = sorted((t for t in threads if t["rate_limits_last"]), key=lambda t: t["started"] or EPOCH)
    if limited:
        first, last = limited[0]["rate_limits_first"], max(limited, key=lambda t: t["ended"] or EPOCH)["rate_limits_last"]
        out.append(
            f"- rate limits ({last.get('plan_type') or 'plan unknown'}): "
            f"5h {window(first, 'primary')} -> {window(last, 'primary')}, "
            f"7d {window(first, 'secondary')} -> {window(last, 'secondary')}"
        )
    out += [
        "",
        "| Thread | Role | Model / effort | Responses | Uncached in | Cached in | Output | Reasoning | Duration |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    by_model: dict[str, Counter] = defaultdict(Counter)
    model_threads: Counter = Counter()
    for thread, model, usage in rows(threads, include_auto_review):
        label = thread["role"] + (f" ({thread['nickname']})" if thread["nickname"] else "")
        effort = thread["effort"] if model == thread["model"] else "?"
        out.append(
            f"| `{thread['id'][:8]}` | {label} | {model} / {effort or '-'} | {thread['responses'].get(model, 0)} | "
            f"{n(usage['input_tokens'] - usage['cached_input_tokens'])} | {n(usage['cached_input_tokens'])} | "
            f"{n(usage['output_tokens'])} | {n(usage['reasoning_output_tokens'])} | "
            f"{duration(thread['started'], thread['ended'])} |"
        )
        by_model[model].update(usage)
        by_model[model]["responses"] += thread["responses"].get(model, 0)
        model_threads[model] += 1

    total: Counter = Counter()
    out += ["", "| Model | Threads | Responses | Uncached in | Cached in | Output | Reasoning |", "|---|---:|---:|---:|---:|---:|---:|"]
    for model, usage in sorted(by_model.items()):
        total.update(usage)
        out.append(
            f"| {model} | {model_threads[model]} | {usage['responses']} | "
            f"{n(usage['input_tokens'] - usage['cached_input_tokens'])} | {n(usage['cached_input_tokens'])} | "
            f"{n(usage['output_tokens'])} | {n(usage['reasoning_output_tokens'])} |"
        )
    out.append(
        f"| **all** | {sum(model_threads.values())} | {total['responses']} | "
        f"{n(total['input_tokens'] - total['cached_input_tokens'])} | {n(total['cached_input_tokens'])} | "
        f"{n(total['output_tokens'])} | {n(total['reasoning_output_tokens'])} |"
    )
    if total["input_tokens"]:
        out += ["", f"Cached share of input: {100 * total['cached_input_tokens'] / total['input_tokens']:.1f}%"]
    skipped = [t for t in threads if t not in counted]
    if skipped:
        out += ["", f"Excluded {len(skipped)} Codex auto-review thread(s); pass --include-auto-review to count them."]
    return "\n".join(out)


def render_json(session_id: str, threads: list[dict]) -> str:
    def encode(value):
        return value.isoformat() if isinstance(value, datetime) else str(value)

    return json.dumps({"session_id": session_id, "threads": threads}, indent=2, default=encode)


def render_csv(session_id: str, threads: list[dict], include_auto_review: bool) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["session_id", "thread_id", "role", "model", "effort", "responses", *FIELDS, "seconds"])
    for thread, model, usage in rows(threads, include_auto_review):
        seconds = (
            int((thread["ended"] - thread["started"]).total_seconds()) if thread["started"] and thread["ended"] else ""
        )
        writer.writerow(
            [session_id, thread["id"], thread["role"], model, thread["effort"] or "", thread["responses"].get(model, 0)]
            + [usage.get(field, 0) for field in FIELDS]
            + [seconds]
        )
    return buffer.getvalue().rstrip("\n")


# --------------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sessions-dir", type=Path, default=None, help="default: $CODEX_HOME/sessions or ~/.codex/sessions")
    commands = parser.add_subparsers(dest="command")

    list_cmd = commands.add_parser("sessions", help="list sessions, newest first")
    list_cmd.add_argument("--date", help="only scan one day (YYYY-MM-DD); much faster")
    list_cmd.add_argument("--limit", type=int, default=20)
    list_cmd.add_argument("--all", action="store_true", help="include sessions without subagents")

    report_cmd = commands.add_parser("report", help="usage report for one session")
    report_cmd.add_argument("session", nargs="?", help="session id or unique prefix (default: newest with subagents)")
    report_cmd.add_argument("--date", help="only scan one day (YYYY-MM-DD); much faster")
    report_cmd.add_argument("--format", choices=("md", "json", "csv"), default="md")
    report_cmd.add_argument("--include-auto-review", action="store_true", help="count Codex guardian threads in totals")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2

    sessions_dir = args.sessions_dir or default_sessions_dir()
    if not sessions_dir.is_dir():
        print(f"sessions directory not found: {sessions_dir}", file=sys.stderr)
        return 2
    groups = group_sessions(sessions_dir, args.date)
    if not groups:
        print("no rollout files found", file=sys.stderr)
        return 1

    if args.command == "sessions":
        listing = sorted(
            ((session_start(sid, items), sid, subagent_count(sid, items), items[0][1].get("cwd")) for sid, items in groups.items()),
            reverse=True,
        )
        print(f"{'started (UTC)':<17}  {'session':<8}  {'subagents':>9}  cwd")
        shown = 0
        for started, sid, subs, cwd in listing:
            if subs or args.all:
                print(f"{started:%Y-%m-%d %H:%M}  {sid[:8]}  {subs:>9}  {cwd}")
                shown += 1
                if shown >= args.limit:
                    break
        if not shown:
            print("(no sessions with subagents; pass --all to include solo sessions)")
        return 0

    if args.session:
        matches = [sid for sid in groups if sid.startswith(args.session)]
        if len(matches) != 1:
            print(f"'{args.session}' matched {len(matches)} sessions; use a longer prefix", file=sys.stderr)
            return 2
        session_id = matches[0]
    else:
        candidates = [(session_start(sid, items), sid) for sid, items in groups.items() if subagent_count(sid, items)]
        if not candidates:
            print("no session with subagents found; pass a session id to report on a solo session", file=sys.stderr)
            return 1
        session_id = max(candidates)[1]

    threads = [analyze(path, meta) for path, meta in groups[session_id]]
    threads.sort(key=lambda t: (t["role"] != "root", t["started"] or EPOCH))
    if args.format == "json":
        print(render_json(session_id, threads))
    elif args.format == "csv":
        print(render_csv(session_id, threads, args.include_auto_review))
    else:
        print(render_md(session_id, threads, args.include_auto_review))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
