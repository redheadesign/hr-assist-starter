"""Load only the context needed for one task; never scan document archives."""
import argparse
import json
from workspace import local_path, read_json


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("onboarding", "application", "profile", "market", "monitor", "feedback", "incident"))
    parser.add_argument("--platform", default="")
    parser.add_argument("--query", default="")
    args = parser.parse_args()
    if args.mode == "onboarding":
        files = ["onboarding/state.json"]
    elif args.mode in ("application", "profile"):
        files = ["config/profile.json", "config/settings.json", "knowledge/facts.json"]
    elif args.mode == "monitor":
        files = ["config/adapters.json", "config/permissions.json"]
    elif args.mode == "incident":
        entries = read_json(local_path("knowledge/incidents/index.json"), [])
        matches = [e for e in entries if (not args.platform or e.get("platform") == args.platform) and (not args.query or args.query.casefold() in json.dumps(e, ensure_ascii=False).casefold())]
        print(json.dumps(matches[:10], ensure_ascii=False, indent=2))
        raise SystemExit(0)
    else:
        files = ["config/settings.json"]
    result = {name: read_json(local_path(name)) for name in files}
    if args.platform and "config/adapters.json" in result:
        all_adapters = (result["config/adapters.json"] or {}).get("adapters", {})
        result["config/adapters.json"] = {args.platform: all_adapters.get(args.platform)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
