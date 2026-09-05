"""Load only the context needed for one task; never scan document archives."""
import argparse
import json
from workspace import local_path, read_json


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("onboarding", "application", "letter", "profile", "market", "monitor", "feedback", "incident", "handoff"))
    parser.add_argument("--platform", default="")
    parser.add_argument("--query", default="")
    args = parser.parse_args()
    if args.mode == "onboarding":
        files = ["onboarding/state.json"]
    elif args.mode in ("application", "letter", "profile"):
        files = ["config/profile.json", "config/settings.json", "knowledge/facts.json", "config/permissions.json"]
    elif args.mode == "monitor":
        files = ["config/adapters.json", "config/permissions.json"]
    elif args.mode == "incident":
        entries = read_json(local_path("knowledge/incidents/index.json"), [])
        matches = [e for e in entries if (not args.platform or e.get("platform") == args.platform) and (not args.query or args.query.casefold() in json.dumps(e, ensure_ascii=False).casefold())]
        print(json.dumps(matches[:10], ensure_ascii=False, indent=2))
        raise SystemExit(0)
    elif args.mode == "handoff":
        path = local_path("knowledge/HANDOFF.md")
        print(path.read_text(encoding="utf-8") if path.exists() else "No pending handoff; run setup.py status")
        raise SystemExit(0)
    else:
        files = ["config/settings.json"]
    result = {name: read_json(local_path(name)) for name in files}
    if args.mode in ("market", "monitor") and local_path("config/settings.json").exists():
        from datetime import datetime
        from workspace import timezone_for_workspace
        from hr_workflow import day_status
        result['day_state'] = day_status(datetime.now(timezone_for_workspace()).date().isoformat())
    if args.platform and args.mode in ('application', 'letter', 'monitor'):
        from hr_policy import authorize, normalize_platform
        args.platform = normalize_platform(args.platform)
        result['authorization'] = authorize('monitor' if args.mode == 'monitor' else 'application', args.platform)
    if args.platform and "config/adapters.json" in result:
        all_adapters = (result["config/adapters.json"] or {}).get("adapters", {})
        result["config/adapters.json"] = {key: value for key, value in all_adapters.items()
            if normalize_platform(value.get('platform', key)) == args.platform}
    print(json.dumps(result, ensure_ascii=False, indent=2))
