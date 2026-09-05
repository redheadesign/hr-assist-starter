#!/usr/bin/env python3
"""Append-only operational store for the HR Assist workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
import threading
from contextlib import contextmanager
from functools import wraps
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from workspace import local_path, timezone_for_workspace
from hr_policy import normalize_platform, read_config
from hr_analytics import analyze, positive, experiment_status, render as render_analytics


ROOT = local_path("")
DATA_DIR = local_path("data")
ADAPTERS_PATH = local_path("config/adapters.json")
WORKSPACE_TZ = timezone_for_workspace()

FILES = {
    "vacancy": DATA_DIR / "vacancies.jsonl",
    "application": DATA_DIR / "applications.jsonl",
    "event": DATA_DIR / "events.jsonl",
    "feedback": DATA_DIR / "feedback.jsonl",
    "experiment": DATA_DIR / "experiments.jsonl",
    "outreach": DATA_DIR / "outreach-targets.jsonl",
    "suppression": DATA_DIR / "suppressions.jsonl",
    "adapter_scan": DATA_DIR / "adapter-scans.jsonl",
}

EVENT_TYPES = {
    "sent",
    "viewed",
    "reply",
    "screening",
    "interview",
    "test",
    "offer",
    "rejection",
    "withdrawn",
    "closed",
    "follow_up",
    "no_response",
}
REJECTION_TYPES = {
    "auto_bot",
    "generic_human",
    "reasoned",
    "duplicate",
    "closed_or_frozen",
    "eligibility",
    "unknown",
}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
ADAPTER_STATUSES = {
    "available",
    "auth_required",
    "captcha_required",
    "layout_changed",
    "temporary_error",
    "disabled_until_requested",
    "sourcing_only",
    "planned",
    "unsupported",
}
APPLICATION_ORIGINS = {"user_confirmed", "platform_observed"}

STAGE_ORDER = ("sent", "viewed", "reply", "screening", "interview", "test", "offer")
_TRANSACTION_MUTEX = threading.RLock()
_TRANSACTION_LOCAL = threading.local()


@contextmanager
def transaction():
    """Serialize the entire read/check/write sequence across threads/processes.

Nested store helpers reuse the outer lock; standalone helpers are safe too.
"""
    with _TRANSACTION_MUTEX:
        if getattr(_TRANSACTION_LOCAL, "active", False):
            yield
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with FileLock(DATA_DIR / ".write.lock"):
            _TRANSACTION_LOCAL.active = True
            try:
                yield
            finally:
                _TRANSACTION_LOCAL.active = False


def transactional(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with transaction():
            return function(*args, **kwargs)
    return wrapped


class StoreError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(WORKSPACE_TZ).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    stamp = datetime.now(WORKSPACE_TZ).strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def text_fingerprint(value: str) -> str:
    normalized = " ".join(value.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def ensure_files() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for path in FILES.values():
        if not path.exists():
            path.touch()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StoreError(f"Файл не найден: {path}") from exc
    except json.JSONDecodeError as exc:
        raise StoreError(f"Некорректный JSON {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise StoreError(f"{path.name}: корень должен быть объектом")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@transactional
def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StoreError(f"{path.name}:{line_number}: некорректный JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise StoreError(f"{path.name}:{line_number}: строка должна содержать объект")
            records.append(value)
    return records


def read_payload(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StoreError(f"Некорректный входной JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise StoreError("Вход должен быть JSON-объектом")
    return payload


class FileLock:
    def __init__(self, path: Path, timeout: float = 5.0) -> None:
        self.path = path
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> "FileLock":
        # OS locks are released even when a process is terminated. Keep the
        # lock file in place: unlinking it could split concurrent lock owners.
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        if os.fstat(self.fd).st_size == 0:
            os.write(self.fd, b"0")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                os.lseek(self.fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise StoreError(f"Не удалось получить блокировку {self.path.name}")
                time.sleep(0.1)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.fd is not None:
            try:
                os.lseek(self.fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)


@transactional
def append_record(kind: str, record: dict[str, Any]) -> None:
    ensure_files()
    path = FILES[kind]
    serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def require(payload: dict[str, Any], fields: Iterable[str]) -> None:
    missing = [field for field in fields if payload.get(field) in (None, "", [])]
    if missing:
        raise StoreError("Не заполнены обязательные поля: " + ", ".join(missing))


def latest_by_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = record.get("id")
        if isinstance(record_id, str):
            result[record_id] = record
    return result


@transactional
def append_event_if_new(record: dict[str, Any]) -> tuple[bool, str]:
    source_event_key = record.get("source_event_key")
    if source_event_key:
        for existing in load_jsonl(FILES["event"]):
            if existing.get("source_event_key") == source_event_key:
                return False, str(existing.get("id"))
    append_record("event", record)
    return True, str(record["id"])


def command_init(_: argparse.Namespace) -> int:
    ensure_files()
    print(f"База готова: {DATA_DIR}")
    return 0


@transactional
def command_add_vacancy(args: argparse.Namespace) -> int:
    payload = read_payload(args.json_path)
    require(payload, ("source", "company", "title"))
    payload["source"] = normalize_platform(payload["source"])
    records = load_jsonl(FILES["vacancy"])
    source_url = payload.get("source_url")
    if source_url:
        for record in reversed(records):
            if record.get("source_url") == source_url:
                print(json.dumps({"status": "exists", "id": record.get("id")}, ensure_ascii=False))
                return 0

    timestamp = now_iso()
    record = {
        "id": payload.pop("id", new_id("vac")),
        "captured_at": payload.pop("captured_at", timestamp),
        "first_seen_at": payload.pop("first_seen_at", timestamp),
        "last_seen_at": payload.pop("last_seen_at", timestamp),
        "status": payload.pop("status", "new"),
        **payload,
    }
    append_record("vacancy", record)
    print(json.dumps({"status": "created", "id": record["id"]}, ensure_ascii=False))
    return 0


@transactional
def command_confirm_sent(args: argparse.Namespace) -> int:
    payload = read_payload(args.json_path)
    require(payload, ("vacancy_id", "channel"))
    payload["channel"] = normalize_platform(payload.get("submission_channel", payload["channel"]))
    payload["submission_channel"] = payload["channel"]
    vacancies = latest_by_id(load_jsonl(FILES["vacancy"]))
    if payload["vacancy_id"] not in vacancies:
        raise StoreError(f"Вакансия {payload['vacancy_id']} не найдена")

    applications = load_jsonl(FILES["application"])
    for record in applications:
        if record.get("vacancy_id") == payload["vacancy_id"] and normalize_platform(record.get("channel", "")) == payload["channel"]:
            raise StoreError(f"Отправка уже записана: {record.get('id')}")

    timestamp = payload.pop("sent_at", now_iso())
    record = {
        "id": payload.pop("id", new_id("app")),
        "sent_at": timestamp,
        "observed_at": payload.pop("observed_at", timestamp),
        "record_origin": "user_confirmed",
        "confirmed_by_user": True,
        "include_in_period_metrics": True,
        "date_quality": "reported",
        "status": payload.pop("status", "sent"),
        **payload,
    }
    append_record("application", record)
    append_record(
        "event",
        {
            "id": new_id("evt"),
            "application_id": record["id"],
            "at": timestamp,
            "observed_at": timestamp,
            "type": "sent",
            "source": record.get("channel"),
            "actor": "candidate",
            "detail": "Отправку подтвердил пользователь",
            "confidence": "high",
            "date_quality": "reported",
        },
    )
    print(json.dumps({"status": "created", "id": record["id"]}, ensure_ascii=False))
    return 0


def _find_or_create_observed_vacancy(payload: dict[str, Any], timestamp: str) -> str:
    source = str(payload["source"])
    company = str(payload["company"])
    title = str(payload["title"])
    vacancy_url = payload.get("vacancy_url")
    vacancies = load_jsonl(FILES["vacancy"])
    if payload.get("vacancy_id"):
        if payload["vacancy_id"] not in latest_by_id(vacancies):
            raise StoreError("Неизвестная vacancy_id")
        return str(payload["vacancy_id"])
    for record in reversed(vacancies):
        if vacancy_url and record.get("source_url") == vacancy_url:
            return str(record["id"])

    record = {
        "id": new_id("vac"),
        "captured_at": timestamp,
        "first_seen_at": timestamp,
        "last_seen_at": timestamp,
        "source": source,
        "source_url": vacancy_url,
        "source_application_key": payload.get("source_application_key"),
        "source_urls": [vacancy_url] if vacancy_url else [],
        "company": company,
        "title": title,
        "status": "applied",
        "captured_from_platform": True,
    }
    for field in ("segment", "vacancy_summary", "level", "location", "work_format", "salary",
                  "matched_fact_ids", "canonical_key", "requirements", "risks", "score", "eligibility"):
        if payload.get(field) not in (None, "", []):
            record[field] = payload[field]
    append_record("vacancy", record)
    return str(record["id"])


def ensure_observed_sent_event(record: dict) -> None:
    """Repair interruption after writing the application but before its event."""
    if any(e.get("application_id") == record["id"] and e.get("type") == "sent"
           for e in load_jsonl(FILES["event"])):
        return
    source = normalize_platform(record.get("source", record.get("channel", "")))
    at = record.get("sent_at") or record["observed_at"]
    append_event_if_new({"id": new_id("evt"), "application_id": record["id"], "at": at,
                         "observed_at": record["observed_at"], "type": "sent", "source": source,
                         "source_url": record.get("source_url"), "source_application_key": record["source_application_key"],
                         "source_event_key": f"{source}:{record['source_application_key']}:sent",
                         "actor": "platform", "detail": "Отклик обнаружен на площадке", "confidence": "high",
                         "date_quality": record["date_quality"], "imported": record.get("imported", False)})


@transactional
def command_observe_application(args: argparse.Namespace) -> int:
    payload = read_payload(args.json_path)
    require(payload, ("source", "source_application_key", "company", "title"))
    source = normalize_platform(payload["source"])
    submission_channel = normalize_platform(payload.get("submission_channel", source))
    payload["source"] = source
    if payload.get("discovery_source"):
        payload["discovery_source"] = normalize_platform(payload["discovery_source"])
    source_application_key = str(payload["source_application_key"])
    observed_at = str(payload.get("observed_at") or now_iso())
    applications = load_jsonl(FILES["application"])
    existing_application: dict[str, Any] | None = None
    for record in reversed(applications):
        if ((normalize_platform(record.get("source", "")) == source and record.get("source_application_key") == source_application_key)
                or (payload.get("canonical_key") and record.get("canonical_key") == payload["canonical_key"])):
            existing_application = record
            break
    vacancy_id = str(existing_application["vacancy_id"]) if existing_application else _find_or_create_observed_vacancy(payload, observed_at)
    if existing_application is None:
        for record in reversed(applications):
            if record.get("vacancy_id") == vacancy_id and normalize_platform(record.get("source") or record.get("channel", "")) == source:
                existing_application = record
                break

    if existing_application is not None:
        if existing_application.get("record_origin") == "platform_observed":
            ensure_observed_sent_event(existing_application)
        print(
            json.dumps(
                {"status": "exists", "id": existing_application.get("id"), "vacancy_id": vacancy_id},
                ensure_ascii=False,
            )
        )
        return 0

    sent_at = payload.get("sent_at")
    date_quality = str(payload.get("date_quality") or ("reported" if sent_at else "observed_only"))
    record = {
        "id": payload.get("id") or new_id("app"),
        "vacancy_id": vacancy_id,
        "sent_at": sent_at,
        "observed_at": observed_at,
        "record_origin": "platform_observed",
        "confirmed_by_user": False,
        "include_in_period_metrics": bool(payload.get("include_in_period_metrics", bool(sent_at))),
        "date_quality": date_quality,
        "channel": submission_channel,
        "submission_channel": submission_channel,
        "source": source,
        "source_application_key": source_application_key,
        "source_url": payload.get("application_url"),
        "status": payload.get("status", "sent"),
        "imported": bool(payload.get("imported", False)),
    }
    for old, new in (("campaign", "campaign_id"), ("variant_id", "variant")):
        if payload.get(old) and not payload.get(new):
            payload[new] = payload[old]
    for field in ("cover_letter_text", "cover_letter_style", "cover_letter_issue_flags",
                  "campaign_id", "experiment_id", "variant", "discovery_source",
                  "profile_version", "fact_version", "matched_fact_ids", "cover_hash",
                  "copy_review_ref", "artifact_ref", "canonical_key", "evidence", "packet_ref"):
        if payload.get(field) not in (None, "", []):
            record[field] = payload[field]
    if record.get("cover_letter_text"):
        record["cover_letter_char_count"] = len(str(record["cover_letter_text"]))
        record["cover_letter_source"] = payload.get("cover_letter_source", "platform_observed")
    append_record("application", record)

    ensure_observed_sent_event(record)
    print(json.dumps({"status": "created", "id": record["id"], "vacancy_id": vacancy_id}, ensure_ascii=False))
    return 0


@transactional
def command_add_event(args: argparse.Namespace) -> int:
    payload = read_payload(args.json_path)
    require(payload, ("application_id", "type"))
    if payload.get("source"):
        payload["source"] = normalize_platform(payload["source"])
    if payload["type"] not in EVENT_TYPES:
        raise StoreError("Неизвестный тип события: " + str(payload["type"]))
    applications = latest_by_id(load_jsonl(FILES["application"]))
    if payload["application_id"] not in applications:
        raise StoreError(f"Отклик {payload['application_id']} не найден")

    confidence = str(payload.pop("confidence", "high"))
    if confidence not in CONFIDENCE_LEVELS:
        raise StoreError("Неизвестный уровень уверенности: " + confidence)
    event_type = str(payload["type"])
    if event_type == "rejection":
        rejection_type = str(payload.get("rejection_type", "unknown"))
        if rejection_type not in REJECTION_TYPES:
            raise StoreError("Неизвестный тип отказа: " + rejection_type)
        payload["rejection_type"] = rejection_type
        if rejection_type == "auto_bot":
            message_text = payload.pop("message_text", None)
            raw_text = payload.pop("raw_text", None) or message_text or payload.get("detail")
            payload["detail"] = "Шаблонный автоотказ"
            if raw_text:
                payload["template_fingerprint"] = text_fingerprint(str(raw_text))
            payload["template_text_omitted"] = True

    observed_at = str(payload.pop("observed_at", now_iso()))
    occurred_at = payload.pop("occurred_at", None)
    at = str(payload.pop("at", occurred_at or observed_at))
    record = {
        "id": payload.pop("id", new_id("evt")),
        "at": at,
        "observed_at": observed_at,
        "date_quality": payload.pop("date_quality", "reported" if occurred_at else "observed_only"),
        "confidence": confidence,
        **payload,
    }
    created, record_id = append_event_if_new(record)
    print(json.dumps({"status": "created" if created else "exists", "id": record_id}, ensure_ascii=False))
    return 0


@transactional
def command_add_feedback(args: argparse.Namespace) -> int:
    payload = read_payload(args.json_path)
    require(payload, ("application_id", "raw_text"))
    applications = latest_by_id(load_jsonl(FILES["application"]))
    if payload["application_id"] not in applications:
        raise StoreError(f"Отклик {payload['application_id']} не найден")
    rejection_type = payload.get("rejection_type")
    if rejection_type and rejection_type not in REJECTION_TYPES:
        raise StoreError("Неизвестный тип отказа: " + str(rejection_type))
    if rejection_type == "auto_bot":
        raw_text = str(payload.pop("raw_text"))
        payload.pop("message_text", None)
        if "detail" in payload:
            payload["detail"] = "Шаблонный автоотказ"
        payload["raw_text_fingerprint"] = text_fingerprint(raw_text)
        payload["raw_text_omitted"] = True
    record = {
        "id": payload.pop("id", new_id("fb")),
        "at": payload.pop("at", now_iso()),
        "confidence": payload.pop("confidence", "high"),
        **payload,
    }
    append_record("feedback", record)
    print(json.dumps({"status": "created", "id": record["id"]}, ensure_ascii=False))
    return 0


@transactional
def command_record_adapter_scan(args: argparse.Namespace) -> int:
    payload = read_payload(args.json_path)
    require(payload, ("adapter", "status"))
    status = str(payload["status"])
    if status not in ADAPTER_STATUSES:
        raise StoreError("Неизвестный статус адаптера: " + status)

    config = load_json(ADAPTERS_PATH)
    adapters = config.get("adapters")
    if not isinstance(adapters, dict) or payload["adapter"] not in adapters:
        raise StoreError("Неизвестный адаптер: " + str(payload["adapter"]))
    adapter = adapters[payload["adapter"]]
    if not isinstance(adapter, dict):
        raise StoreError("Конфигурация адаптера должна быть объектом")

    timestamp = str(payload.get("at") or now_iso())
    needs_user_action = bool(
        payload.get("needs_user_action", status in {"auth_required", "captcha_required"})
    )
    failure_reason = payload.get("failure_reason")
    scan_succeeded = bool(payload.get("scan_succeeded", False))
    enable_read = bool(payload.get("enable_read", False))
    if scan_succeeded and status != "available":
        raise StoreError("Успешный скан требует доступного состояния площадки")
    if enable_read and (status != "available" or not scan_succeeded):
        raise StoreError("Чтение можно включить только после успешного доступного прогона")

    scan_record = {
        "id": payload.get("id") or new_id("scan"),
        "adapter": payload["adapter"],
        "at": timestamp,
        "status": status,
        "scan_succeeded": scan_succeeded,
        "failure_reason": failure_reason,
        "needs_user_action": needs_user_action,
        "items_seen": int(payload.get("items_seen", 0)),
        "events_added": int(payload.get("events_added", 0)),
        "duplicates_skipped": int(payload.get("duplicates_skipped", 0)),
        "scheduled_run": bool(payload.get("scheduled_run", False)),
    }
    append_record("adapter_scan", scan_record)

    adapter["status"] = status
    adapter["last_attempt_at"] = timestamp
    adapter["failure_reason"] = failure_reason
    adapter["needs_user_action"] = needs_user_action
    if status == "available":
        adapter["last_auth_ok_at"] = timestamp
    if scan_succeeded:
        adapter["last_successful_scan_at"] = timestamp
        remaining = int(adapter.get("control_runs_remaining", 0))
        control_day = parse_iso(timestamp).date().isoformat()
        if remaining > 0 and payload.get("scheduled_run") is True and adapter.get("last_counted_control_day") != control_day:
            adapter["control_runs_remaining"] = remaining - 1
            adapter["last_counted_control_day"] = control_day
    if enable_read:
        adapter["read_enabled"] = True
        adapter["initial_probe_completed"] = True
    config["updated_at"] = datetime.now(WORKSPACE_TZ).date().isoformat()
    write_json_atomic(ADAPTERS_PATH, config)
    print(json.dumps({"status": "updated", "adapter": payload["adapter"]}, ensure_ascii=False))
    return 0


def command_adapter_status(_: argparse.Namespace) -> int:
    config = load_json(ADAPTERS_PATH)
    result: dict[str, Any] = {}
    for name, value in config.get("adapters", {}).items():
        if isinstance(value, dict) and value.get("transport") == "browser":
            result[name] = {
                key: value.get(key)
                for key in (
                    "status",
                    "read_enabled",
                    "needs_user_action",
                    "failure_reason",
                    "last_auth_ok_at",
                    "last_successful_scan_at",
                    "control_runs_remaining",
                    "target_url",
                )
            }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=WORKSPACE_TZ)
    return parsed.astimezone(WORKSPACE_TZ)


def period_from_args(args: argparse.Namespace) -> tuple[datetime, datetime]:
    end = datetime.now(WORKSPACE_TZ)
    if args.until:
        end_date = date.fromisoformat(args.until)
        end = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), WORKSPACE_TZ)
    if args.since:
        start = datetime.combine(date.fromisoformat(args.since), datetime.min.time(), WORKSPACE_TZ)
    else:
        start = end - timedelta(days=args.days)
    return start, end


def pct(value: int, total: int) -> str:
    return "0%" if not total else f"{value / total * 100:.1f}%"


@transactional
def build_summary(start: datetime, end: datetime, *, as_of: datetime | None = None, window_days: int | None = None) -> str:
    as_of = as_of or datetime.now(WORKSPACE_TZ)
    window_days = window_days if window_days is not None else read_config("workflow.json")["response_window_days"]
    vacancies = latest_by_id(load_jsonl(FILES["vacancy"]))
    all_applications = load_jsonl(FILES["application"])
    applications = [
        item
        for item in all_applications
        if item.get("sent_at")
        and item.get("include_in_period_metrics", True) is not False
        and item.get("date_quality") != "observed_only"
        and start <= parse_iso(str(item["sent_at"])) < end
        and parse_iso(str(item["sent_at"])) <= as_of
    ]
    app_ids = {item["id"] for item in applications if item.get("id")}
    events = [item for item in load_jsonl(FILES["event"]) if item.get("application_id") in app_ids
              and item.get("at") and parse_iso(str(item["at"])) <= as_of]
    feedback = [item for item in load_jsonl(FILES["feedback"]) if item.get("application_id") in app_ids
                and item.get("at") and parse_iso(str(item["at"])) <= as_of]

    event_types_by_app: dict[str, set[str]] = defaultdict(set)
    rejection_type_by_app: dict[str, str] = {}
    for item in events:
        app_id = item.get("application_id")
        event_type = item.get("type")
        if isinstance(app_id, str) and isinstance(event_type, str):
            event_types_by_app[app_id].add(event_type)
            if event_type == "rejection":
                rejection_type_by_app[app_id] = str(item.get("rejection_type", "unknown"))
    for item in feedback:
        app_id = item.get("application_id")
        if isinstance(app_id, str) and item.get("rejection_type"):
            rejection_type_by_app[app_id] = str(item["rejection_type"])

    total = len(applications)
    stage_counts = {
        stage: sum(1 for app in applications if stage in event_types_by_app.get(str(app.get("id")), set()))
        for stage in STAGE_ORDER
    }
    positive_ids = {
        item.get("application_id") for item in events
        if positive(item)
    }
    positive_count = sum(1 for app in applications if app.get("id") in positive_ids)
    bot_screenings = len({item.get("application_id") for item in events
                          if item.get("type") == "screening" and item.get("actor") == "bot"})
    rejections = sum(
        1 for app in applications if "rejection" in event_types_by_app.get(str(app.get("id")), set())
    )
    auto_rejections = sum(1 for value in rejection_type_by_app.values() if value == "auto_bot")
    reasoned_rejections = sum(1 for value in rejection_type_by_app.values() if value == "reasoned")

    channels = Counter(str(item.get("channel", "unknown")) for item in applications)
    segments: Counter[str] = Counter()
    for app in applications:
        vacancy = vacancies.get(str(app.get("vacancy_id")), {})
        segments[str(vacancy.get("segment", "unknown"))] += 1
    rejection_types = Counter(rejection_type_by_app.values())

    lines = [
        f"# Воронка: {start.date().isoformat()} – {(end - timedelta(microseconds=1)).date().isoformat()}",
        "",
        f"Отправлено: **{total}**",
        f"Содержательный ответ или следующий этап: **{positive_count}** ({pct(positive_count, total)})",
        f"Бот-скрининги (отдельно от содержательных ответов): **{bot_screenings}**",
        f"Интервью: **{stage_counts['interview']}** ({pct(stage_counts['interview'], total)})",
        f"Офферы: **{stage_counts['offer']}** ({pct(stage_counts['offer'], total)})",
        f"Отказы: **{rejections}** ({pct(rejections, total)})",
        f"Автоотказы: **{auto_rejections}**",
        f"Отказы с причиной: **{reasoned_rejections}**",
        "",
        "## Этапы",
        "",
    ]
    for stage in STAGE_ORDER:
        lines.append(f"- {stage}: {stage_counts[stage]} ({pct(stage_counts[stage], total)})")

    lines.extend(["", "## Каналы", ""])
    lines.extend(f"- {name}: {count}" for name, count in channels.most_common()) if channels else lines.append("- Пока нет отправок")

    lines.extend(["", "## Сегменты", ""])
    lines.extend(f"- {name}: {count}" for name, count in segments.most_common()) if segments else lines.append("- Пока нет данных")

    lines.extend(["", "## Типы отказов", ""])
    lines.extend(f"- {name}: {count}" for name, count in rejection_types.most_common()) if rejection_types else lines.append("- Пока нет классифицированных отказов")

    lines.extend(
        [
            "",
            "Отклики без известной даты отправки исключены из периодической конверсии. Свежим откликам нужно время на ответ.",
            "",
        ]
    )
    lines.append(render_analytics(analyze(applications, vacancies, events, start, end, as_of, window_days, feedback=feedback)))
    return "\n".join(lines)


def command_summary(args: argparse.Namespace) -> int:
    start, end = period_from_args(args)
    as_of = parse_iso(args.as_of) if args.as_of else datetime.now(WORKSPACE_TZ)
    if args.as_json:
        with transaction():
            result = analyze(load_jsonl(FILES["application"]), latest_by_id(load_jsonl(FILES["vacancy"])),
                             load_jsonl(FILES["event"]), start, end, as_of, args.window_days,
                             feedback=load_jsonl(FILES["feedback"]))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(build_summary(start, end, as_of=as_of, window_days=args.window_days))
    return 0


def command_experiment_status(args: argparse.Namespace) -> int:
    experiments = latest_by_id(load_jsonl(FILES["experiment"]))
    if args.experiment_id not in experiments:
        raise StoreError("Неизвестный experiment_id")
    result = experiment_status(experiments[args.experiment_id], load_jsonl(FILES["application"]),
                               parse_iso(args.as_of) if args.as_of else datetime.now(WORKSPACE_TZ))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_dashboard(args: argparse.Namespace) -> int:
    start, end = period_from_args(args)
    output = local_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_summary(start, end, as_of=parse_iso(args.as_of) if args.as_of else None,
                                    window_days=args.window_days), encoding="utf-8")
    print(output)
    return 0


def command_validate(_: argparse.Namespace) -> int:
    ensure_files()
    errors: list[str] = []
    loaded: dict[str, list[dict[str, Any]]] = {}
    for kind, path in FILES.items():
        try:
            loaded[kind] = load_jsonl(path)
        except StoreError as exc:
            errors.append(str(exc))
            loaded[kind] = []

    for kind, records in loaded.items():
        ids = [str(item.get("id")) for item in records if item.get("id")]
        duplicates = [record_id for record_id, count in Counter(ids).items() if count > 1]
        if duplicates:
            errors.append(f"{FILES[kind].name}: повторяющиеся id: {', '.join(duplicates)}")

    vacancy_ids = {item.get("id") for item in loaded["vacancy"]}
    application_ids = {item.get("id") for item in loaded["application"]}
    for item in loaded["application"]:
        if item.get("vacancy_id") not in vacancy_ids:
            errors.append(f"applications.jsonl: неизвестная vacancy_id {item.get('vacancy_id')}")
        origin = item.get("record_origin", "user_confirmed" if item.get("confirmed_by_user") else None)
        if origin not in APPLICATION_ORIGINS:
            errors.append(f"applications.jsonl: неизвестный record_origin {origin}")
        if origin == "user_confirmed" and item.get("confirmed_by_user") is not True:
            errors.append(f"applications.jsonl: {item.get('id')} не подтвержден пользователем")
        if origin == "platform_observed":
            if item.get("confirmed_by_user") is not False:
                errors.append(f"applications.jsonl: {item.get('id')} неверный флаг platform_observed")
            if not item.get("source_application_key"):
                errors.append(f"applications.jsonl: {item.get('id')} нет source_application_key")

    source_event_keys: Counter[str] = Counter()
    for item in loaded["event"]:
        if item.get("application_id") not in application_ids:
            errors.append(f"events.jsonl: неизвестная application_id {item.get('application_id')}")
        if item.get("type") not in EVENT_TYPES:
            errors.append(f"events.jsonl: неизвестный type {item.get('type')}")
        if item.get("confidence", "high") not in CONFIDENCE_LEVELS:
            errors.append(f"events.jsonl: неизвестный confidence {item.get('confidence')}")
        if item.get("rejection_type") and item.get("rejection_type") not in REJECTION_TYPES:
            errors.append(f"events.jsonl: неизвестный rejection_type {item.get('rejection_type')}")
        if item.get("source_event_key"):
            source_event_keys[str(item["source_event_key"])] += 1
    repeated_event_keys = [key for key, count in source_event_keys.items() if count > 1]
    if repeated_event_keys:
        errors.append("events.jsonl: повторяющиеся source_event_key: " + ", ".join(repeated_event_keys))

    for item in loaded["feedback"]:
        if item.get("application_id") not in application_ids:
            errors.append(f"feedback.jsonl: неизвестная application_id {item.get('application_id')}")
        if item.get("rejection_type") and item.get("rejection_type") not in REJECTION_TYPES:
            errors.append(f"feedback.jsonl: неизвестный rejection_type {item.get('rejection_type')}")

    try:
        adapter_config = load_json(ADAPTERS_PATH)
        for name, adapter in adapter_config.get("adapters", {}).items():
            if not isinstance(adapter, dict):
                errors.append(f"adapters.json: {name} должен быть объектом")
                continue
            if adapter.get("status") not in ADAPTER_STATUSES:
                errors.append(f"adapters.json: {name} имеет неизвестный status")
            if adapter.get("status") == "unsupported" and adapter.get("read_enabled"):
                errors.append(f"adapters.json: {name} не поддерживается, read_enabled должен быть false")
            if adapter.get("transport") == "browser" and not adapter.get("target_url"):
                errors.append(f"adapters.json: {name} не содержит target_url")
    except StoreError as exc:
        errors.append(str(exc))

    if errors:
        for error in errors:
            print("ERROR: " + error)
        return 1
    counts = {kind: len(records) for kind, records in loaded.items()}
    print(json.dumps({"ok": True, "records": counts}, ensure_ascii=False))
    return 0


def add_period_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--since", help="YYYY-MM-DD")
    parser.add_argument("--until", help="YYYY-MM-DD включительно")
    parser.add_argument("--as-of", help="Момент среза событий ISO, отдельно от даты когорты")
    parser.add_argument("--window-days", type=int, default=read_config("workflow.json")["response_window_days"])


def add_json_command(subparsers: Any, name: str, handler: Any) -> None:
    command = subparsers.add_parser(name)
    command.add_argument("--json", required=True, dest="json_path")
    command.set_defaults(handler=handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Локальная база HR Assist")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.set_defaults(handler=command_init)

    add_json_command(subparsers, "add-vacancy", command_add_vacancy)
    add_json_command(subparsers, "confirm-sent", command_confirm_sent)
    add_json_command(subparsers, "observe-application", command_observe_application)
    add_json_command(subparsers, "add-event", command_add_event)
    add_json_command(subparsers, "add-feedback", command_add_feedback)
    add_json_command(subparsers, "record-adapter-scan", command_record_adapter_scan)

    adapter_parser = subparsers.add_parser("adapter-status")
    adapter_parser.set_defaults(handler=command_adapter_status)

    summary_parser = subparsers.add_parser("summary")
    add_period_args(summary_parser)
    summary_parser.add_argument("--json", action="store_true", dest="as_json")
    summary_parser.set_defaults(handler=command_summary)

    experiment_parser = subparsers.add_parser("experiment-status")
    experiment_parser.add_argument("--id", dest="experiment_id", required=True)
    experiment_parser.add_argument("--as-of")
    experiment_parser.set_defaults(handler=command_experiment_status)

    dashboard_parser = subparsers.add_parser("dashboard")
    add_period_args(dashboard_parser)
    dashboard_parser.add_argument("--output", default="DASHBOARD.md")
    dashboard_parser.set_defaults(handler=command_dashboard)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.set_defaults(handler=command_validate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except (StoreError, OSError, ValueError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
