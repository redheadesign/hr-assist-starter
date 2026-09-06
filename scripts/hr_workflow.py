#!/usr/bin/env python3
"""Durable day coverage, company evidence, blockers and reviewable send packets."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

from workspace import ROOT as CODE_ROOT, local_path
import hr_store
from hr_policy import authorize, eligibility, normalize_platform, read_config

ROOT = local_path("")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def load_state(root=ROOT):
    path = root / "data/workflow-state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "days": {}, "companies": {}, "blockers": {}}


def expected_sources(root=ROOT, day=None):
    config = json.loads((root / "config/settings.json").read_text(encoding="utf-8"))
    companies = json.loads((root / "config/company-career-sources.json").read_text(encoding="utf-8"))["companies"]
    day = day or datetime.now(hr_store.WORKSPACE_TZ).date().isoformat()
    weekday = datetime.strptime(day, "%Y-%m-%d").weekday()
    raw_cadence = config["campaign"].get("source_cadence", {})
    if not isinstance(raw_cadence, dict):
        raise ValueError("source_cadence must be a mapping")
    cadence = {}
    for key, rule in raw_cadence.items():
        canonical = normalize_platform(key)
        if canonical in cadence:
            raise ValueError("Duplicate cadence aliases")
        if not isinstance(rule, dict) or rule.get("frequency") not in {"daily", "weekly", "manual"}:
            raise ValueError("Source cadence requires daily, weekly or manual frequency")
        if rule["frequency"] == "weekly":
            days = rule.get("weekdays")
            if not isinstance(days, list) or not days or any(type(d) is not int or not 0 <= d <= 6 for d in days):
                raise ValueError("Weekly cadence requires weekdays 0..6 (Monday..Sunday)")
        cadence[canonical] = rule
    enabled = set()
    for source in config["campaign"]["sources"]:
        key = normalize_platform(source["id"])
        rule = cadence.get(key, {"frequency": "daily"})
        if rule["frequency"] == "daily" or (rule["frequency"] == "weekly" and weekday in rule["weekdays"]):
            enabled.add(key)
    expected = enabled - {"company_careers"}
    if "company_careers" in enabled:
        expected.update("company:" + c.get("scan_group", c["id"]) for c in companies)
    return sorted(expected)


def day_status(day, *, root=ROOT):
    state = load_state(root)
    recorded = state["days"].get(day, {})
    adapters = json.loads((root / "config/adapters.json").read_text(encoding="utf-8"))["adapters"]
    inboxes = {normalize_platform(a["platform"]) for a in adapters.values()
               if a.get("transport") == "browser" and a.get("read_enabled") and a.get("initial_probe_completed")}
    coverage = {}
    for field, expected in (("market_coverage", expected_sources(root, day)), ("inbox_coverage", inboxes)):
        observed = recorded.get(field, {})
        per_source = {s: observed.get(s, {"state": "unknown"}) for s in sorted(expected)}
        coverage[field] = {"state": "not_required" if not per_source else "complete" if all(s.get("state") == "complete" for s in per_source.values()) else "partial",
                           "sources": per_source}
    apps = hr_store.load_jsonl(root / "data/applications.jsonl")
    sent = {a.get("canonical_key") or a.get("id") for a in apps
            if a.get("sent_at", "") and hr_store.parse_iso(a["sent_at"]).astimezone(hr_store.WORKSPACE_TZ).date().isoformat() == day and a.get("date_quality") != "observed_only"
            and a.get("record_origin") in {"platform_observed", "user_confirmed"}}
    search = json.loads((root / "config/settings.json").read_text(encoding="utf-8"))
    target = search["campaign"]["daily_minimum"] or 0
    ready = recorded.get("ready_to_send")
    queue_path = root / "data/campaign-state.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else {}
    unresolved = [t for t in queue.get("tasks", []) if t.get("day") == day
                  and t.get("type") in {"application", "source_scan"}
                  and t.get("status") not in {"completed", "failed"}]
    completed = (coverage["market_coverage"]["state"] in {"complete", "not_required"} and ready == 0
                 and recorded.get("pool_exhausted") is True and not unresolved)
    campaign = "complete" if completed and len(sent) >= target else "complete_with_shortfall" if completed and recorded.get("shortfall_reason") else "in_progress"
    if not search["campaign"].get("enabled"):
        campaign = "disabled"
    if recorded.get("campaign_state") == "stopped_by_user":
        campaign = "stopped_by_user"
    maximum = search["campaign"].get("daily_maximum")
    return {"day": day, "today_sent_count": len(sent), "daily_maximum": maximum,
            "send_budget_remaining": max(0, maximum - len(sent)) if maximum is not None else None, "target_min": target,
            "target_reached": len(sent) >= target, "campaign_state": campaign, **coverage,
            "ready_to_send": ready, "shortfall_reason": recorded.get("shortfall_reason"),
            "unresolved_tasks": [{k: t.get(k) for k in ("id", "type", "status")} for t in unresolved],
            "report_exists": (root / f"reports/daily/{day}.md").exists(),
            "next_action": "configure_or_run_requested_task" if campaign == "disabled" else
                           "respect_user_stop" if campaign == "stopped_by_user" else
                           "continue_sources_or_pending_sends" if campaign == "in_progress" else "monitor_inboxes",
            "source_stats": recorded.get("source_stats", {}), "blockers": state["blockers"]}


def record_day(state, payload):
    day = payload["day"]
    datetime.strptime(day, "%Y-%m-%d")
    if not payload.get("artifact_ref"):
        raise ValueError("day update requires artifact_ref")
    entry = state["days"].setdefault(day, {})
    if "campaign_state" in payload:
        if payload["campaign_state"] != "stopped_by_user":
            raise ValueError("Only an explicit user stop may override computed campaign state")
        entry["campaign_state"] = "stopped_by_user"
    for field in ("market_coverage", "inbox_coverage"):
        for source, observed in payload.get(field, {}).items():
            if observed.get("state") not in {"complete", "partial", "error", "unknown"}:
                raise ValueError("Invalid coverage state")
            if not observed.get("evidence"):
                raise ValueError("Coverage update requires evidence")
            entry.setdefault(field, {})[normalize_platform(source)] = observed
    if "ready_to_send" in payload and (not isinstance(payload["ready_to_send"], int) or payload["ready_to_send"] < 0):
        raise ValueError("ready_to_send must be nonnegative")
    for source, stats in payload.get("source_stats", {}).items():
        entry.setdefault("source_stats", {}).setdefault(normalize_platform(source), {}).update(stats)
    for field in ("ready_to_send", "pool_exhausted", "shortfall_reason", "artifact_ref"):
        if field in payload:
            entry[field] = payload[field]
    entry["updated_at"] = hr_store.now_iso()
    return entry


def record_company(state, payload, now, cache_days):
    for key in ("company_id", "risk_context", "evidence", "checked_at", "safe"):
        if key not in payload or payload[key] in (None, "", []):
            raise ValueError("Missing company evidence: " + key)
    checked = hr_store.parse_iso(payload["checked_at"])
    if checked > now or not isinstance(payload["safe"], bool):
        raise ValueError("Invalid company check")
    entry = {**payload, "risk_context_hash": digest(payload["risk_context"]),
             "expires_at": (checked + timedelta(days=cache_days)).isoformat()}
    state["companies"][payload["company_id"]] = entry
    return entry


def cached_company(state, company_id, risk_context, now):
    entry = state["companies"].get(company_id)
    fresh = bool(entry and entry.get("risk_context_hash") == digest(risk_context)
                 and hr_store.parse_iso(entry["expires_at"]) > now)
    return {"usable": fresh, "check": entry if fresh else None}


def record_blocker(state, payload):
    key = normalize_platform(payload["platform"])
    previous = state["blockers"].get(key, {})
    reason = payload.get("reason")
    changed = reason != previous.get("reason")
    state["blockers"][key] = {"reason": reason, "observed_at": payload.get("observed_at", hr_store.now_iso()),
                              "notified": bool(payload.get("notified")) if changed else bool(previous.get("notified") or payload.get("notified")),
                              "evidence": payload.get("evidence", previous.get("evidence"))}
    return {"notify": bool(reason and not state["blockers"][key]["notified"]), "changed": changed}


def build_packet(payload, *, root=ROOT):
    """Build once, bind lint/review to exactly this copy and current facts/rules.

Review is supplied by the independent reviewer; this function never invents a
pass or sends anything. A worker verifies the packet again before submission.
"""
    platform = normalize_platform(payload["platform"])
    decision = authorize("application", platform)
    vacancy = payload["vacancy"]
    selected = eligibility(vacancy)
    text = payload["cover_letter_text"]
    config = read_config("channels.json")
    profile = json.loads((root / "config/profile.json").read_text(encoding="utf-8"))
    facts = json.loads((root / "knowledge/facts.json").read_text(encoding="utf-8"))
    validator_path = CODE_ROOT / "scripts/validate_copy.py"
    spec = importlib.util.spec_from_file_location("packet_copy_validator", validator_path)
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    maximum = payload.get("maximum_chars")
    fact_version = digest(facts)
    fingerprint = digest({"text": text, "fact_version": fact_version, "profile": profile,
                          "channel": platform, "maximum": maximum, "length_unit": payload.get("length_unit", "characters"),
                          "channels": config, "validator_version": digest(validator_path.read_text(encoding="utf-8")),
                          "vacancy": vacancy, "policy": read_config("permissions.json"),
                          "search": read_config("settings.json"), "resume": payload.get("resume_ref"),
                          "matched_fact_ids": payload.get("matched_fact_ids"), "canonical_key": payload.get("canonical_key"),
                          "vacancy_url": payload.get("vacancy_url"), "official_route": payload.get("official_route"),
                          "experiment_id": payload.get("experiment_id"), "variant": payload.get("variant")})
    cached_validation = payload.get("copy_validation", {})
    if payload.get("review_fingerprint") == fingerprint and cached_validation.get("fingerprint") == fingerprint:
        copy_result = cached_validation
    else:
        copy_result = {**validator.validate(text, platform, maximum_chars=maximum, length_unit=payload.get("length_unit", "characters")),
                       "fingerprint": fingerprint}
    review = payload.get("review", {})
    review_valid = bool(review.get("fingerprint") == fingerprint and review.get("outcome") == "pass"
                        and review.get("reviewer") and review.get("artifact_ref"))
    missing = []
    for key in ("canonical_key", "vacancy_url", "resume_ref", "matched_fact_ids", "campaign_id"):
        if not payload.get(key):
            missing.append(key)
    public_facts = {f["id"]: f for f in facts if f.get("id")
                    and f.get("visibility") == "public" and f.get("confirmed") is True}
    fact_ids = payload.get("matched_fact_ids", [])
    if any(fact_id not in public_facts for fact_id in fact_ids):
        missing.append("confirmed_public_fact_ids")
    resume_refs = read_config("permissions.json").get("standing_application_authorization", {}).get("resume_refs", {})
    expected_resume = resume_refs.get(platform)
    if not expected_resume or payload.get("resume_ref") != expected_resume:
        missing.append("authorized_resume")
    if (expected_resume and not str(expected_resume).startswith(('https://', 'http://'))
            and read_config("permissions.json").get("automatic_uploads") is not True):
        missing.append("file_upload_authorization")
    if platform == "company_careers":
        route = payload.get("official_route", {})
        if not route.get("evidence_url") or not route.get("verified_at") or route.get("target_url") != payload.get("vacancy_url"):
            missing.append("verified_official_route")
    score = vacancy.get("score")
    meets_score = isinstance(score, (int, float)) and score >= read_config("settings.json")["campaign"]["eligibility_floor_score"]
    return {**payload, "platform": platform, "authorization": decision, "eligibility": selected,
            "matched_facts": [public_facts[f] for f in fact_ids if f in public_facts],
            "profile_version": digest(profile), "fact_version": fact_version,
            "cover_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(), "review_fingerprint": fingerprint,
            "copy_validation": copy_result, "review_valid": review_valid, "missing": missing,
            "ready": bool(decision["allowed"] and selected["decision"] == "eligible" and meets_score
                          and copy_result["ok"] and review_valid and not missing)}


def evidence_valid(evidence, vacancy_key, config=None):
    config = config or read_config("workflow.json")["submission_evidence"]
    return bool(all(evidence.get(k) for k in config["required_fields"])
                and evidence["kind"] in config["accepted_kinds"]
                and evidence["status"] in config["accepted_statuses"]
                and evidence["vacancy_key"] == vacancy_key)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["day", "record-day", "record-company", "company", "blocker", "packet", "evidence"])
    p.add_argument("--json", dest="json_path")
    p.add_argument("--day", default=datetime.now(hr_store.WORKSPACE_TZ).date().isoformat())
    args = p.parse_args()
    payload = json.loads(Path(args.json_path).read_text(encoding="utf-8")) if args.json_path else {}
    if args.command == "day":
        result = day_status(args.day)
    elif args.command == "packet":
        result = build_packet(payload)
    elif args.command == "evidence":
        result = {"valid": evidence_valid(payload["evidence"], payload["canonical_key"])}
    else:
        with hr_store.transaction():
            state = load_state()
            if args.command == "record-day":
                result = record_day(state, payload)
            elif args.command == "record-company":
                result = record_company(state, payload, datetime.now(hr_store.WORKSPACE_TZ), read_config("workflow.json")["company_cache_days"])
            elif args.command == "company":
                result = cached_company(state, payload["company_id"], payload["risk_context"], datetime.now(hr_store.WORKSPACE_TZ))
            else:
                result = record_blocker(state, payload)
            if args.command != "company":
                hr_store.write_json_atomic(ROOT / "data/workflow-state.json", state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
