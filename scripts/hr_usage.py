#!/usr/bin/env python3
"""Aggregate privacy-safe usage from rollout files selected explicitly by the caller."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from workspace import local_path, write_json


USAGE_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens", "total_tokens")


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def token_counters(value):
    """Keep only documented numeric counters, never event text or metadata."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return {"total_tokens": value}
    if not isinstance(value, dict):
        return {}
    counters = {name: value[name] for name in USAGE_FIELDS
                if isinstance(value.get(name), int) and not isinstance(value[name], bool) and value[name] >= 0}
    return counters if counters else {}


def parse_usage(path, through=None):
    """Read one explicitly supplied JSONL rollout and return aggregate-safe metrics."""
    path = Path(path)
    through = parse_time(through) if isinstance(through, str) else through
    if through is not None and through.tzinfo is None:
        raise ValueError("--through must include a UTC offset or Z")
    snapshot = None
    models = {}
    tool_calls = 0
    first, last = None, None
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            timestamp = parse_time(event.get("timestamp"))
            if through is not None and timestamp is not None and timestamp > through:
                continue
            if timestamp is not None:
                first = timestamp if first is None or timestamp < first else first
                last = timestamp if last is None or timestamp > last else last
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if event.get("type") == "turn_context":
                model, effort = payload.get("model"), payload.get("effort")
                if isinstance(model, str) and isinstance(effort, str):
                    models[(model, effort)] = models.get((model, effort), 0) + 1
            elif event.get("type") == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                if isinstance(info, dict):
                    observed = token_counters(info.get("total_token_usage"))
                    if observed:
                        # Codex token_count is cumulative. A second event is a newer
                        # snapshot, not additional token consumption.
                        snapshot = observed
            elif event.get("type") == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
                tool_calls += 1
    return {
        "usage": snapshot,
        "observed_models": [{"model": model, "reasoning_effort": effort, "turns": count}
                            for (model, effort), count in sorted(models.items())],
        "duration_seconds": round((last - first).total_seconds(), 3) if first and last else None,
        "top_level_tool_calls": tool_calls,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rollout", nargs="+", help="Explicit local rollout JSONL files; directories are never scanned")
    parser.add_argument("--through", help="ISO 8601 instant, including Z or an explicit UTC offset")
    parser.add_argument("--output", help="Relative path below .local for the aggregate JSON")
    args = parser.parse_args()
    through = parse_time(args.through) if args.through else None
    if args.through and (through is None or through.tzinfo is None):
        parser.error("--through must be a valid ISO instant with Z or an explicit UTC offset")
    selected = [Path(name).resolve() for name in args.rollout]
    if len(set(selected)) != len(selected):
        parser.error("Each rollout may be selected only once")
    reports = [parse_usage(name, through) for name in selected]
    total = {"files": len(reports), "usage": {}, "observed_models": {}, "duration_seconds": 0.0,
             "top_level_tool_calls": 0}
    saw_duration = False
    for report in reports:
        for name, value in (report["usage"] or {}).items():
            total["usage"][name] = total["usage"].get(name, 0) + value
        for item in report["observed_models"]:
            key = (item["model"], item["reasoning_effort"])
            total["observed_models"][key] = total["observed_models"].get(key, 0) + item["turns"]
        if report["duration_seconds"] is not None:
            total["duration_seconds"] += report["duration_seconds"]
            saw_duration = True
        total["top_level_tool_calls"] += report["top_level_tool_calls"]
    result = {
        "files": total["files"],
        "usage": total["usage"] or None,
        "usage_complete": all(report["usage"] is not None for report in reports),
        "missing_usage_count": sum(report["usage"] is None for report in reports),
        "observed_models": [{"model": model, "reasoning_effort": effort, "turns": count}
                            for (model, effort), count in sorted(total["observed_models"].items())],
        "duration_seconds": round(total["duration_seconds"], 3) if saw_duration else None,
        "top_level_tool_calls": total["top_level_tool_calls"],
    }
    if args.output:
        output = local_path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        write_json(output, result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
