"""Cohort metrics with explicit observation time and response windows."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median
from workspace import local_path, timezone_for_workspace
from hr_policy import normalize_platform

ZONE = timezone_for_workspace()
HUMAN_ACTORS = {"human", "employer", "recruiter"}
NEXT_STAGES = {"interview", "test", "offer"}


def time_of(value):
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t.replace(tzinfo=ZONE) if t.tzinfo is None else t.astimezone(ZONE)


def positive(event):
    return event.get("type") in NEXT_STAGES or (
        event.get("type") in {"reply", "screening"} and event.get("actor") in HUMAN_ACTORS)


def analyze(applications, vacancies, events, start, end, as_of, window_days=14, *, feedback=()):
    if window_days <= 0:
        raise ValueError("response window must be positive")
    apps = [a for a in applications if a.get("sent_at")
            and a.get("include_in_period_metrics", True) is not False
            and a.get("date_quality") != "observed_only"
            and start <= time_of(a["sent_at"]) < end
            and time_of(a["sent_at"]) <= as_of]
    by_app = defaultdict(list)
    for event in events:
        if event.get("at") and time_of(event["at"]) <= as_of:
            by_app[event.get("application_id")].append(event)
    rejection_types = {}
    for event in sorted((e for values in by_app.values() for e in values), key=lambda e: time_of(e["at"])):
        if event.get("type") == "rejection":
            rejection_types[event.get("application_id")] = event.get("rejection_type", "unknown")
    for item in sorted((f for f in feedback if f.get("at") and time_of(f["at"]) <= as_of), key=lambda f: time_of(f["at"])):
        if item.get("rejection_type"):
            rejection_types[item.get("application_id")] = item["rejection_type"]
    rows = []
    for app in apps:
        sent = time_of(app["sent_at"])
        sent_latest = sent.replace(hour=23, minute=59, second=59) if app.get("date_quality") == "platform_day" else sent
        mature = as_of >= sent_latest + timedelta(days=window_days)
        observed = by_app[app["id"]]
        human = [e for e in observed if positive(e)]
        timed = [e for e in human if e.get("date_quality") != "observed_only"
                 and time_of(e["at"]) >= sent]
        window_end = sent + timedelta(days=window_days)
        within = [e for e in timed if (time_of(e["at"]).replace(hour=23, minute=59, second=59)
                                     if e.get("date_quality") == "platform_day" else time_of(e["at"])) <= window_end]
        # A day-precision event at the boundary is conservatively unknown.
        uncertain = any(e.get("date_quality") == "observed_only" for e in human)
        uncertain |= any(e.get("date_quality") == "platform_day" and time_of(e["at"]) <= window_end
                         < time_of(e["at"]).replace(hour=23, minute=59, second=59) for e in timed)
        if app.get("date_quality") == "platform_day":
            boundary = sent.date() + timedelta(days=window_days)
            uncertain |= any(time_of(e["at"]).date() == boundary for e in timed)
            within = [e for e in within if time_of(e["at"]).date() < boundary]
        precise = [e for e in timed if e.get("date_quality") not in {"platform_day", "observed_only"}]
        lag = None
        if precise and app.get("date_quality") != "platform_day":
            lag = (min(time_of(e["at"]) for e in precise) - sent).total_seconds()/86400
        vacancy = vacancies.get(app.get("vacancy_id"), {})
        rows.append({"id": app["id"], "age_days": (as_of-sent).total_seconds()/86400,
                     "mature": mature, "positive": bool(human), "positive_within": bool(within),
                     "timing_unknown": uncertain and not within,
                     "interview": any(e.get("type") == "interview" for e in observed),
                     "offer": any(e.get("type") == "offer" for e in observed),
                     "viewed": any(e.get("type") == "viewed" for e in observed),
                     "test": any(e.get("type") == "test" for e in observed),
                     "human_screening": any(e.get("type") == "screening" and e.get("actor") in HUMAN_ACTORS for e in observed),
                     "closed": any(e.get("type") == "closed" for e in observed),
                     "no_response_as_of": not any(e.get("type") in {"reply", "screening", "interview", "test", "offer", "rejection"}
                                                  and e.get("actor") != "candidate" for e in observed),
                     "rejection": any(e.get("type") == "rejection" for e in observed),
                     "rejection_type": rejection_types.get(app["id"], "unknown"),
                     "interview_within": any(e.get("type") == "interview" for e in within),
                     "bot_screening": any(e.get("type") == "screening" and e.get("actor") == "bot" for e in observed),
                     "first_response_days": lag,
                     "channel": normalize_platform(app.get("submission_channel", app.get("channel", "unknown"))),
                     "discovery_source": app.get("discovery_source", "unknown"),
                     "segment": vacancy.get("segment", "unknown"),
                     "campaign": app.get("campaign_id", app.get("campaign", "unknown")),
                     "variant": ":".join((app.get("experiment_id", "unknown"), app.get("variant", app.get("variant_id", "unknown"))))})
    def summarize(group):
        mature = [r for r in group if r["mature"]]
        known = [r for r in mature if not r["timing_unknown"]]
        lags = [r["first_response_days"] for r in group if r["first_response_days"] is not None]
        return {"sent": len(group), "mature": len(mature), "immature": len(group)-len(mature),
                "window_denominator": len(known), "window_timing_unknown": len(mature)-len(known),
                "positive": sum(r["positive"] for r in group),
                "positive_within_window": sum(r["positive_within"] for r in known),
                "interview": sum(r["interview"] for r in group),
                "interview_within_window": sum(r["interview_within"] for r in known),
                "offer": sum(r["offer"] for r in group), "rejection": sum(r["rejection"] for r in group),
                "bot_screening": sum(r["bot_screening"] for r in group),
                **{key: sum(r[key] for r in group) for key in ("viewed", "test", "human_screening", "closed", "no_response_as_of")},
                "rejection_types": dict(Counter(r["rejection_type"] for r in group if r["rejection"])),
                "median_first_response_days": median(lags) if lags else None,
                "response_time_sample": len(lags)}
    groups = {}
    for dimension in ("channel", "discovery_source", "segment", "campaign", "variant"):
        grouped = defaultdict(list)
        for row in rows:
            grouped[row[dimension]].append(row)
        groups[dimension] = {key: summarize(value) for key,value in sorted(grouped.items())}
    return {"as_of": as_of.isoformat(), "cohort_start": start.isoformat(), "cohort_end_exclusive": end.isoformat(),
            "response_window_days": window_days, "totals": summarize(rows), "groups": groups,
            "age_days": [round(r["age_days"], 2) for r in rows]}


def experiment_status(experiment, applications, as_of):
    if not experiment.get("started_at"):
        return {"id": experiment["id"], "state": "planned", "winner": None}
    start = time_of(experiment["started_at"])
    enrollment_end = start + timedelta(days=experiment["enrollment_days"])
    if experiment.get("ended_at"):
        enrollment_end = min(enrollment_end, time_of(experiment["ended_at"]))
    eligible = [a for a in applications if a.get("experiment_id") == experiment["id"]
                and a.get("variant") in experiment["variants"] and a.get("sent_at")
                and a.get("date_quality") != "observed_only" and a.get("include_in_period_metrics", True)
                and start <= time_of(a["sent_at"]) < enrollment_end and time_of(a["sent_at"]) <= as_of]
    latest = max((time_of(a["sent_at"]).replace(hour=23, minute=59, second=59)
                  if a.get("date_quality") == "platform_day" else time_of(a["sent_at"]) for a in eligible), default=enrollment_end)
    ready_at = max(enrollment_end, latest + timedelta(days=experiment["response_window_days"]))
    counts = {variant: sum(a["variant"] == variant for a in eligible) for variant in experiment["variants"]}
    sufficient = all(n >= experiment["minimum_sends_per_variant"] for n in counts.values())
    state = "enrolling" if as_of < enrollment_end else "maturing" if as_of < ready_at else "ready_for_review" if sufficient else "inconclusive"
    return {"id": experiment["id"], "state": state, "counts": counts,
            "enrollment_end": enrollment_end.isoformat(), "ready_at": ready_at.isoformat(),
            "winner": None, "positive_count_required_to_stop": False}


def render(metrics):
    days = metrics["response_window_days"]
    lines = ["", f"## Сопоставимая конверсия: окно {days} дней", "",
             f"Срез событий: {metrics['as_of']}. Знаменатель: созревшие отклики с известным временем исхода.",
             "Незрелые отклики и неизвестное время ответа показаны отдельно; ноль не заменяет неизвестное.", ""]
    for label, groups in (("Всего", {"все": metrics["totals"]}), *metrics["groups"].items()):
        if not groups:
            continue
        lines.extend([f"### {label}", ""])
        for key, m in groups.items():
            denominator = m["window_denominator"]
            rate = f"{m['positive_within_window'] / denominator:.1%}" if denominator else "не определена"
            lines.append(f"- {key}: отправлено {m['sent']}; созрело {m['mature']}; ответ в окне {m['positive_within_window']}/{denominator} ({rate}); интервью в окне {m['interview_within_window']}/{denominator}; незрелые {m['immature']}; время исхода неизвестно {m['window_timing_unknown']}.")
        lines.append("")
    m = metrics["totals"]
    if m["median_first_response_days"] is not None:
        lines.append(f"Медиана до первого содержательного ответа: {m['median_first_response_days']:.2f} дня, n={m['response_time_sample']}; только известное время отправки и ответа.")
    return "\n".join(lines)
