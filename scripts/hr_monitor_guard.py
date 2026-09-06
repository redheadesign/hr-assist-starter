#!/usr/bin/env python3
"""Durable, coordinator-owned retry gate for inbox/browser monitoring."""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from workspace import local_path, timezone_for_workspace

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = local_path("")
TIMEZONE = timezone_for_workspace()
STARTER = True

STATE_PATH = DATA_ROOT / "data" / "monitor-guard-state.json"
CONFIG_PATH = DATA_ROOT / "config" / "bot-monitor.json"
DEFAULT_POLICY = {"failure_backoff_minutes": [240, 480, 1440], "max_diagnostic_attempts": 1, "unfinished_retry_minutes": 240, "diagnostic_model": "gpt-5.6-sol", "diagnostic_reasoning_effort": "high", "browser_timeout_ms": 60000, "prearm_before_browser": True}


class GuardError(RuntimeError):
    pass


def now_local() -> datetime:
    return datetime.now(TIMEZONE).replace(microsecond=0)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise GuardError("Дата должна содержать часовой пояс")
    return parsed.astimezone(TIMEZONE).replace(microsecond=0)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise GuardError("Дата должна содержать часовой пояс")
    return value.astimezone(TIMEZONE).replace(microsecond=0).isoformat()


def policy(path: Path = CONFIG_PATH) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GuardError("Некорректный JSON bot-monitor.json") from exc
    nested = raw.get("error_policy", {})
    if not isinstance(nested, dict):
        raise GuardError("error_policy должен быть объектом")
    result = dict(DEFAULT_POLICY)
    for key in result:
        value = nested.get(key, raw.get(key, result[key]))
        result[key] = value
    # ``backoff_minutes`` is the public configuration name. Keep the longer
    # internal name for compatibility with early guard callers.
    result["failure_backoff_minutes"] = nested.get(
        "backoff_minutes", raw.get("backoff_minutes", result["failure_backoff_minutes"])
    )
    values = result["failure_backoff_minutes"]
    if not isinstance(values, list) or not values or any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in values):
        raise GuardError("failure_backoff_minutes должен быть непустым списком положительных минут")
    if not isinstance(result["max_diagnostic_attempts"], int) or isinstance(result["max_diagnostic_attempts"], bool) or not 0 <= result["max_diagnostic_attempts"] <= 1:
        raise GuardError("max_diagnostic_attempts должен быть 0 или 1")
    if not isinstance(result["unfinished_retry_minutes"], int) or isinstance(result["unfinished_retry_minutes"], bool) or result["unfinished_retry_minutes"] < 240:
        raise GuardError("unfinished_retry_minutes должен быть не меньше 240")
    if values[0] < 240 or values[-1] > 1440 or values != sorted(values):
        raise GuardError("failure_backoff_minutes должен не убывать в пределах 240..1440")
    if result["diagnostic_model"] != "gpt-5.6-sol" or result["diagnostic_reasoning_effort"] != "high":
        raise GuardError("Диагностика разрешена только для gpt-5.6-sol high")
    return result


def empty_state() -> dict[str, Any]:
    return {"version": 1, "updated_at": None, "attempts": {}, "active_attempt_key": None, "consecutive_failures": 0, "cooldown_until": None, "diagnostic_consumed": 0, "incident_active": False, "checkpoints": {}}


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists(): return empty_state()
    try: state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc: raise GuardError("Некорректный JSON monitor guard") from exc
    if not isinstance(state, dict) or not isinstance(state.get("attempts"), dict): raise GuardError("Некорректное состояние monitor guard")
    for key, value in empty_state().items(): state.setdefault(key, value)
    return state


class Lock:
    def __init__(self, path: Path): self.path, self.fd = path, None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True); deadline = time.monotonic() + 5
        while True:
            try: self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY); return self
            except FileExistsError:
                if time.monotonic() >= deadline: raise GuardError("Не удалось получить блокировку monitor guard")
                time.sleep(.05)
    def __exit__(self, *args):
        if self.fd is not None: os.close(self.fd)
        self.path.unlink(missing_ok=True)


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"); os.replace(tmp, path)


def mutate(path: Path, now: datetime, operation: Callable[[dict[str, Any]], Any]) -> Any:
    with Lock(path.with_suffix(path.suffix + ".lock")):
        state = load_state(path); result = operation(state); state["updated_at"] = iso(now); write_state(path, state); return result


def _require_key(value: str, name: str) -> str:
    value = value.strip()
    if not value: raise GuardError(f"{name} не может быть пустым")
    return value


def _record_error(state: dict[str, Any], attempt: dict[str, Any], now: datetime, cfg: dict[str, Any], code: str, artifact_ref: str) -> None:
    failures = int(state["consecutive_failures"]) + 1; backoff = cfg["failure_backoff_minutes"][min(failures - 1, len(cfg["failure_backoff_minutes"]) - 1)]
    attempt.update({"status": "error", "finished_at": iso(now), "error_code": code, "artifact_ref": artifact_ref})
    state["consecutive_failures"] = failures; state["cooldown_until"] = iso(now + timedelta(minutes=backoff)); state["active_attempt_key"] = None
    if not state["incident_active"]: state["incident_active"] = True; state["diagnostic_consumed"] = 0


def _recover_crash(state: dict[str, Any], now: datetime, cfg: dict[str, Any]) -> None:
    key = state.get("active_attempt_key")
    if not key: return
    attempt = state["attempts"].get(key)
    if attempt and attempt.get("status") == "pending" and parse_time(attempt["retry_not_before"]) <= now:
        _record_error(state, attempt, now, cfg, "unfinished", "")


def _status(state: dict[str, Any], now: datetime, cfg: dict[str, Any]) -> dict[str, Any]:
    key = state.get("active_attempt_key"); active = state["attempts"].get(key) if key else None
    retry = active.get("retry_not_before") if active else state.get("cooldown_until")
    blocked = bool(retry and parse_time(retry) > now)
    expired_pending = bool(active and not blocked)
    minutes = math.ceil((parse_time(retry) - now).total_seconds() / 60) if blocked else 0
    return {"may_scan": not blocked, "recovery_required": expired_pending, "retry_not_before": retry, "interval_minutes_floor": max(0, minutes), "active_attempt": key, "consecutive_failures": state["consecutive_failures"], "diagnostic_available": bool(state["incident_active"] and state["diagnostic_consumed"] < cfg["max_diagnostic_attempts"])}


def status(*, root: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Read the guard without writing it; optional root supports private data roots."""
    base = root if root is not None else DATA_ROOT
    timestamp = now if now is not None else now_local()
    return _status(load_state(base / "data" / "monitor-guard-state.json"), timestamp, policy(base / "config" / "bot-monitor.json"))


def apply_schedule(plan: dict[str, Any], gate: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Make cooldown win over due probes in the executable scheduling result."""
    result = dict(plan)
    floor = gate["interval_minutes_floor"]
    result["retry_guard"] = gate
    if floor > 0:
        interval = max(int(plan["interval_minutes"]), floor)
        result.update(mode="error_backoff" if gate["consecutive_failures"] else "prearmed",
                      interval_minutes=interval, next_run_at=iso(now + timedelta(minutes=interval)),
                      run_now=False)
    return result


def begin(state: dict[str, Any], key: str, now: datetime, cfg: dict[str, Any]) -> dict[str, Any]:
    key = _require_key(key, "attempt-key"); _recover_crash(state, now, cfg)
    existing = state["attempts"].get(key)
    if existing: return {**_status(state, now, cfg), "authorized": False, "scan_authorized": False, "reason": "attempt_exists"}
    snapshot = _status(state, now, cfg)
    if not snapshot["may_scan"]: return {**snapshot, "authorized": False, "scan_authorized": False, "reason": "cooldown"}
    retry = now + timedelta(minutes=cfg["unfinished_retry_minutes"])
    state["attempts"][key] = {"attempt_key": key, "status": "pending", "started_at": iso(now), "retry_not_before": iso(retry)}; state["active_attempt_key"] = key
    return {**_status(state, now, cfg), "authorized": True, "scan_authorized": True}


def evidence(ref: str) -> str:
    ref = _require_key(ref, "artifact-ref"); candidate = Path(ref)
    if STARTER:
        project_ref = (ROOT / ref).resolve() if not candidate.is_absolute() else candidate.resolve()
        local_root = DATA_ROOT.resolve()
        if not project_ref.is_relative_to(local_root): raise GuardError("artifact-ref должен оставаться внутри .local")
    else:
        project_ref = (ROOT / ref).resolve() if not candidate.is_absolute() else candidate.resolve()
        if not project_ref.is_relative_to(ROOT.resolve()): raise GuardError("artifact-ref должен оставаться внутри проекта")
    return ref


def finish(state: dict[str, Any], key: str, outcome: str, error_code: str | None, artifact_ref: str, now: datetime, cfg: dict[str, Any]) -> dict[str, Any]:
    key = _require_key(key, "attempt-key"); artifact_ref = evidence(artifact_ref)
    if outcome not in {"success", "error"}: raise GuardError("outcome: success или error")
    attempt = state["attempts"].get(key)
    if not attempt: raise GuardError("attempt-key не найден")
    if attempt["status"] != "pending": return {"status": "duplicate", "attempt": attempt}
    if state.get("active_attempt_key") != key: raise GuardError("Устаревшая pending попытка не может завершить новый запуск")
    if outcome == "success":
        attempt.update({"status": "success", "finished_at": iso(now), "artifact_ref": artifact_ref}); state.update({"active_attempt_key": None, "consecutive_failures": 0, "cooldown_until": None, "incident_active": False, "diagnostic_consumed": 0})
    else: _record_error(state, attempt, now, cfg, _require_key(error_code or "", "error-code"), artifact_ref)
    return {"status": "recorded", "attempt": attempt}


def diagnose(state: dict[str, Any], key: str, cfg: dict[str, Any]) -> dict[str, Any]:
    _require_key(key, "attempt-key")
    attempt = state["attempts"].get(key)
    if not attempt: raise GuardError("attempt-key не найден")
    latest = max(state["attempts"].values(), key=lambda item: item.get("finished_at", ""))
    if attempt is not latest or attempt.get("status") != "error" or not state["incident_active"] or state["diagnostic_consumed"] >= cfg["max_diagnostic_attempts"]:
        return {"authorized": False, "diagnostic_authorized": False}
    state["diagnostic_consumed"] += 1; return {"authorized": True, "diagnostic_authorized": True, "attempt_key": key, "send_authorized": False}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(); subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("status")
    for name in ("begin", "diagnose"):
        p = subs.add_parser(name); p.add_argument("--attempt-key", required=True)
    p = subs.add_parser("finish"); p.add_argument("--attempt-key", required=True); p.add_argument("--outcome", choices=("success", "error"), required=True); p.add_argument("--error-code"); p.add_argument("--artifact-ref", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args(); now = now_local(); cfg = policy()
    if args.command == "status": print(json.dumps(status(now=now), ensure_ascii=False)); return 0
    fn = {"begin": lambda s: begin(s, args.attempt_key, now, cfg), "finish": lambda s: finish(s, args.attempt_key, args.outcome, args.error_code, args.artifact_ref, now, cfg), "diagnose": lambda s: diagnose(s, args.attempt_key, cfg)}[args.command]
    try: print(json.dumps(mutate(STATE_PATH, now, fn), ensure_ascii=False)); return 0
    except (GuardError, OSError, ValueError) as exc: print("ERROR: " + str(exc)); return 1


if __name__ == "__main__": raise SystemExit(main())
