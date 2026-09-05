"""Idempotent offline bootstrap and resumable, agent-led onboarding."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from workspace import ROOT, local_path, read_json, write_json

VERSION = 1
STAGES = ("basics", "documents", "experience", "preferences", "writing", "channels", "automation", "review")
TABLES = ("vacancies", "applications", "events", "feedback", "experiments", "outreach-targets", "suppressions", "adapter-scans")


def merge_missing(value, defaults):
    """Add new optional fields without replacing existing answers or permissions."""
    if not isinstance(value, dict) or not isinstance(defaults, dict):
        return value
    result = dict(value)
    for key, default in defaults.items():
        result[key] = merge_missing(value[key], default) if key in value else default
    return result


def initialize():
    version_path = local_path("schema-version.json")
    version = read_json(version_path, {"version": VERSION})
    if version.get("version") != VERSION:
        raise ValueError("Unsupported local schema; preserve data and use an explicit migration")
    for name in ("settings", "profile", "permissions", "adapters", "channels", "workflow", "bot-monitor", "company-career-sources"):
        target = local_path(f"config/{name}.json")
        defaults = read_json(ROOT / "templates" / f"{name}.json")
        previous = read_json(target, {})
        updated = merge_missing(previous, defaults)
        if not target.exists() or updated != previous:
            write_json(target, updated)
    for folder in ("documents", "photos", "artifacts", "reports/daily", "reports/weekly", "reports/resume-review", "inputs", "drafts", "knowledge/incidents", "onboarding"):
        local_path(folder).mkdir(parents=True, exist_ok=True)
    for table in TABLES:
        path = local_path(f"data/{table}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch()
    for name, value in (("knowledge/facts.json", []), ("knowledge/incidents/index.json", []), ("documents/manifest.json", []), ("onboarding/state.json", {"schema_version": VERSION, "stages": {}, "ready": False})):
        target = local_path(name)
        if not target.exists():
            write_json(target, value)
    write_json(version_path, {"version": VERSION})
    return status()


def status():
    state = read_json(local_path("onboarding/state.json"), {})
    done = state.get("stages", {})
    return {**state, "next_stage": next((s for s in STAGES if s not in done), None)}


def record(stage, payload=None, skip=False):
    initialize()
    payload = payload or {}
    if not isinstance(payload, dict):
        raise ValueError("Stage payload must be a JSON object")
    profile_path = local_path("config/profile.json")
    settings_path = local_path("config/settings.json")
    profile = read_json(profile_path)
    settings = read_json(settings_path)
    allowed = {
        "basics": {"name", "headline", "location", "contacts", "links", "language", "timezone"},
        "documents": {"notes"},
        "experience": {"summary", "experience", "education", "skills", "languages"},
        "preferences": {"target"}, "writing": {"writing"}, "channels": {"channels"},
        "automation": {"requested"}, "review": {"notes"},
    }
    if set(payload) - allowed[stage]:
        raise ValueError("Unexpected stage fields; do not put secrets in onboarding")
    if not skip:
        for key, value in payload.items():
            if key in profile:
                if value is not None and not isinstance(value, type(profile[key])) and profile[key] is not None:
                    raise ValueError(f"Wrong type: {key}")
                profile[key] = value
            elif key in ("language", "timezone", "channels"):
                settings[key] = value
            elif key in ("target", "writing"):
                if not isinstance(value, dict) or set(value) - set(settings[key]):
                    raise ValueError(f"Unknown configuration: {key}")
                for field, item in value.items():
                    previous = settings[key][field]
                    if isinstance(previous, dict):
                        if not isinstance(item, dict) or set(item) - set(previous):
                            raise ValueError(f"Unknown nested configuration: {field}")
                        previous.update(item)
                    elif previous is not None and not isinstance(item, type(previous)):
                        raise ValueError(f"Wrong configuration type: {field}")
                    else:
                        settings[key][field] = item
            elif stage == "automation" and key == "requested":
                # A request is not authorization and never installs scheduled jobs.
                settings["schedule"]["requested"] = value
        if not isinstance(settings["timezone"], str):
            raise ValueError("Timezone must be an IANA name")
        from zoneinfo import ZoneInfo
        ZoneInfo(settings["timezone"])
        if not isinstance(settings["channels"], list):
            raise ValueError("Channels must be a list")
        write_json(profile_path, profile)
        write_json(settings_path, settings)
        write_json(local_path(f"onboarding/{stage}.json"), payload)
    state = read_json(local_path("onboarding/state.json"))
    state["stages"][stage] = {"status": "skipped" if skip else "completed", "at": datetime.now(timezone.utc).isoformat()}
    state["ready"] = all(s in state["stages"] for s in STAGES) and state["stages"].get("review", {}).get("status") == "completed"
    state["partial"] = any(v["status"] == "skipped" for v in state["stages"].values())
    write_json(local_path("onboarding/state.json"), state)
    return status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "status", "save", "skip"))
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--json", dest="input_path")
    args = parser.parse_args()
    if args.command in ("save", "skip") and not args.stage:
        parser.error("--stage is required")
    if args.command == "save" and not args.input_path:
        parser.error("save requires --json with a local payload")
    if args.command == "init":
        result = initialize()
    elif args.command == "status":
        result = status()
    else:
        payload = json.loads(Path(args.input_path).read_text(encoding="utf-8")) if args.input_path else None
        result = record(args.stage, payload, args.command == "skip")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
