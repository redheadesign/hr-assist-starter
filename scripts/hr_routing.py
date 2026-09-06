"""Validate economical scheduled routes; never silently inherit the parent model."""
import argparse
import json
from workspace import local_path, read_json

ROLES = {"coordinator", "text", "scout", "company_checker", "reviewer",
         "application_worker", "bot_dispatcher", "bot_responder", "weekly_review", "resume_review"}
EFFORTS = {
    "gpt-5.6-luna": {"low", "medium", "high", "xhigh", "max"},
    "gpt-5.6-terra": {"low", "medium", "high", "xhigh", "max", "ultra"},
    "gpt-5.6-sol": {"low", "medium", "high", "xhigh", "max", "ultra"},
}


def validate(config):
    if not isinstance(config, dict) or config.get("version") != 1 or config.get("scope") != "scheduled_hr_jobs":
        raise ValueError("Expected model-routing version 1 for scheduled_hr_jobs; run setup.py init")
    for key in ("forbidden_models", "scheduled_allowed_models"):
        values = config.get(key)
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
            raise ValueError(key + " must be a nonempty list of model names")
    forbidden = set(config["forbidden_models"]) | {"gpt-6-astra"}
    allowed = set(config["scheduled_allowed_models"])
    if allowed & forbidden or allowed - EFFORTS.keys():
        raise ValueError("Scheduled allowlist includes forbidden or unsupported models")
    routes = config.get("routes")
    if not isinstance(routes, dict) or set(routes) != ROLES:
        raise ValueError("Every scheduled role must have an explicit route")
    for role, route in routes.items():
        if not isinstance(route, dict) or not isinstance(route.get("fallbacks", []), list):
            raise ValueError("Invalid route: " + role)
        for step in [route, *route.get("fallbacks", [])]:
            if not isinstance(step, dict):
                raise ValueError("Invalid fallback: " + role)
            model, effort = step.get("model"), step.get("reasoning_effort")
            if not isinstance(model, str) or model not in allowed or model in forbidden:
                raise ValueError("Forbidden or unsupported model in " + role)
            if not isinstance(effort, str) or effort not in EFFORTS[model]:
                raise ValueError("Unsupported reasoning effort in " + role)
    limits = config.get("worker_limits")
    if not isinstance(limits, dict) or limits.get("fork_turns") != "none" or limits.get("nested_delegation") is not False:
        raise ValueError("Workers require fork_turns=none and no nested delegation")
    for key, maximum in (("max_active", 4), ("max_sites_per_scout", 4), ("max_applications_per_worker", 4),
                         ("handoff_wait_seconds", 60), ("no_progress_seconds", 180), ("no_progress_browser_calls", 8)):
        value = limits.get(key)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(key + " is missing or exceeds the economical bound")
    return config


def load_config():
    return validate(read_json(local_path("config/model-routing.json")))


def resolve(config, role, available=None, fallback_index=0):
    """Availability is supplied by the caller after inspecting its actual runtime."""
    validate(config)
    route = config["routes"][role]
    steps = [route, *route.get("fallbacks", [])]
    if not 0 <= fallback_index < len(steps):
        raise ValueError("Fallback index outside configured route")
    for index, step in enumerate(steps[fallback_index:], start=fallback_index):
        if available is None or step["model"] in available:
            return {"role": role, "model": step["model"], "reasoning_effort": step["reasoning_effort"],
                    "route_index": index, "availability_verified": available is not None,
                    "fork_turns": "none"}
    raise ValueError("No available configured model; stop this role instead of inheriting Astra")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "route"))
    parser.add_argument("--role", choices=sorted(ROLES))
    parser.add_argument("--available-model", action="append", dest="available")
    parser.add_argument("--fallback-index", type=int, default=0)
    args = parser.parse_args()
    try:
        config = load_config()
        if args.command == "route" and not args.role:
            parser.error("route requires --role")
        result = {"valid": True} if args.command == "validate" else resolve(config, args.role, args.available, args.fallback_index)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, KeyError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
