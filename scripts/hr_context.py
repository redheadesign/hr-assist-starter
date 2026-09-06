"""Small read-only task contexts; bot checks do not scan the daily CRM."""
import argparse
import json
from workspace import local_path, read_json
from hr_policy import authorize, normalize_platform


def compact_day(value):
    result = {k: v for k, v in value.items() if k not in {"source_stats", "blockers"}}
    for field in ("market_coverage", "inbox_coverage"):
        result[field] = {"state": value[field]["state"], "sources": {
            key: {"state": item.get("state", "unknown")} for key, item in value[field]["sources"].items()}}
    result["blockers"] = {key: {k: item.get(k) for k in ("platform", "reason", "notified")}
                          for key, item in value.get("blockers", {}).items() if item.get("reason")}
    return result


def adapter_context(platform="", full=False):
    adapters = (read_json(local_path("config/adapters.json"), {}) or {}).get("adapters", {})
    fields = ("platform", "transport", "mode", "read_enabled", "initial_probe_completed", "status",
              "target_url", "login_url", "needs_user_action", "checkpoint", "control_runs_remaining")
    return {key: item if full else {k: item[k] for k in fields if k in item}
            for key, item in adapters.items()
            if not platform or normalize_platform(item.get("platform", key)) == platform}


def bot_context(platform="", full=False):
    from hr_campaign import load_state, load_monitor_config, now_local, schedule_mode, due_post_campaign_probes
    from hr_monitor_guard import apply_schedule, status as guard_status
    state, timing, now = load_state(), load_monitor_config(), now_local()
    config = read_json(local_path("config/bot-monitor.json"), {})
    adapters = adapter_context(platform, full)
    for adapter in adapters.values():
        key = normalize_platform(adapter.get("platform", ""))
        adapter["target_url"] = config.get("inbox_urls", {}).get(key, adapter.get("target_url"))
    platforms = sorted({normalize_platform(a.get("platform", key)) for key, a in adapters.items()})
    if platform and platform not in platforms:
        platforms.append(platform)
    gate = guard_status(now=now)
    schedule = apply_schedule(schedule_mode(state, now, **timing), gate, now)
    active_ids = set(schedule["active_chat_ids"])
    return {"enabled": config.get("enabled", False), "schedule": schedule,
            "due_probes": due_post_campaign_probes(state, now),
            "active_chats": [{k: t.get(k) for k in ("id", "platform", "status", "next_check_at", "idle_deadline_at")}
                             for t in state["tasks"] if t.get("id") in active_ids
                             and (not platform or normalize_platform(t.get("platform", "")) == platform)],
            "adapters": adapters,
            "authorization": {p: {op: authorize(op, p) for op in ("monitor", "bot_reply")} for p in platforms},
            "checkpoint": read_json(local_path("data/bot-monitor-state.json"), {}),
            "retry_guard": gate,
            "routing_config": ".local/config/model-routing.json",
            "next_guide": "Read docs/bot-dialog.md only for a new confirmed bot question"}


def build_context(mode, platform="", full=False, query=""):
    platform = normalize_platform(platform) if platform else ""
    if mode == "bot-monitor":
        return bot_context(platform, full)
    if mode == "onboarding":
        files = ["onboarding/state.json"]
    elif mode in ("application", "letter", "profile"):
        files = ["config/profile.json", "config/settings.json", "knowledge/facts.json", "config/permissions.json"]
    elif mode == "monitor":
        files = ["config/permissions.json"] if full else []
    elif mode == "incident":
        entries = read_json(local_path("knowledge/incidents/index.json"), [])
        return [e for e in entries if (not platform or normalize_platform(e.get("platform", "")) == platform)
                and (not query or query.casefold() in json.dumps(e, ensure_ascii=False).casefold())][:10]
    else:
        files = ["config/settings.json"]
    result = {name: read_json(local_path(name)) for name in files}
    if mode == "monitor":
        adapters = adapter_context(platform, full)
        result["config/adapters.json"] = adapters
        result["read_authorization"] = {normalize_platform(a.get("platform", key)): authorize("monitor", a.get("platform", key))
                                        for key, a in adapters.items()}
    if mode in ("market", "monitor") and local_path("config/settings.json").exists():
        from datetime import datetime
        from workspace import timezone_for_workspace
        from hr_workflow import day_status
        day = day_status(datetime.now(timezone_for_workspace()).date().isoformat())
        result["day_state"] = day if full else compact_day(day)
        result["routing_config"] = ".local/config/model-routing.json"
    if platform and mode in ("application", "letter", "monitor"):
        result["authorization"] = authorize("monitor" if mode == "monitor" else "application", platform)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("onboarding", "application", "letter", "profile", "market", "monitor", "bot-monitor", "feedback", "incident", "handoff"))
    parser.add_argument("--platform", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--full", action="store_true", help="Include full daily evidence and statistics")
    args = parser.parse_args()
    if args.mode == "handoff":
        path = local_path("knowledge/HANDOFF.md")
        print(path.read_text(encoding="utf-8") if path.exists() else "No pending handoff; run setup.py status")
    else:
        print(json.dumps(build_context(args.mode, args.platform, args.full, args.query), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
