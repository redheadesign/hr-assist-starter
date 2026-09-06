#!/usr/bin/env python3
"""Durable, coordinator-owned task queue for daily HR campaigns.

Workers must not call mutating commands. The coordinator creates and claims
tasks, gives the returned claim token to a worker, and records only the
worker's artifact reference with ``finish``. Raw messages, cookies, tokens and
other secrets do not belong in this state file.

Short bot-chat checks (10/30/60 seconds) happen inside a worker. After those
checks, ``defer-chat`` schedules durable checks every 5 minutes for at most one
hour since the last new incoming message or actually sent response. Merely
polling a chat never extends that deadline. When no chat is active, the external
app scheduler may use ``schedule-mode`` to fall back to a four-hour cadence.

Once an application batch is actually complete, ``post-campaign`` stores one
idempotent probe series for its canonical campaign key. Inbox-only probes are
due at configured offsets (normally +5, +10 and +60 minutes); one late scan can
acknowledge every probe already due. A failed scan is recorded as an attempt,
not retried forever.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from workspace import local_path, timezone_for_workspace
from hr_policy import normalize_platform


ROOT = local_path("")
STATE_PATH = local_path("data/campaign-state.json")
MONITOR_CONFIG_PATH = local_path("config/bot-monitor.json")
WORKSPACE_TZ = timezone_for_workspace()
TASK_TYPES = {"source_scan", "application", "bot_chat"}
RISKY_TASK_TYPES = {"application", "bot_chat"}
ACTIVE_CHAT_STATUSES = {"pending", "running", "waiting"}
ACTIVE_POLL_SECONDS = 5 * 60
IDLE_POLL_SECONDS = 4 * 60 * 60
IDLE_TIMEOUT_SECONDS = 60 * 60
MAX_HANDOFF_LEASE_SECONDS = 15 * 60
POST_CAMPAIGN_PROBE_MINUTES = [5, 10, 60]
DEFAULT_MONITOR_CONFIG = {
    "idle_check_minutes": IDLE_POLL_SECONDS // 60,
    "active_check_minutes": ACTIVE_POLL_SECONDS // 60,
    "inactivity_timeout_minutes": IDLE_TIMEOUT_SECONDS // 60,
    "short_wait_seconds": [10, 30, 60],
    "post_campaign_probe_minutes": POST_CAMPAIGN_PROBE_MINUTES,
}


class CampaignError(RuntimeError):
    pass


def now_local() -> datetime:
    return datetime.now(WORKSPACE_TZ).replace(microsecond=0)


def parse_time(value: str | None) -> datetime:
    if value is None:
        return now_local()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=WORKSPACE_TZ)
    return parsed.astimezone(WORKSPACE_TZ).replace(microsecond=0)


def iso(value: datetime) -> str:
    return value.astimezone(WORKSPACE_TZ).replace(microsecond=0).isoformat()


def normalize_key(value: str) -> str:
    """Normalize a caller-provided vacancy/chat identity, never its platform."""
    return " ".join(value.casefold().split())


def load_monitor_config(path: Path = MONITOR_CONFIG_PATH) -> dict[str, Any]:
    """Load the scheduler timing contract without changing the config file."""
    config = dict(DEFAULT_MONITOR_CONFIG)
    config["short_wait_seconds"] = list(DEFAULT_MONITOR_CONFIG["short_wait_seconds"])
    config["post_campaign_probe_minutes"] = list(
        DEFAULT_MONITOR_CONFIG["post_campaign_probe_minutes"]
    )
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CampaignError(f"Некорректный JSON {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise CampaignError("bot-monitor config должен быть JSON-объектом")
        for key in config:
            if key in raw:
                config[key] = raw[key]
    for key in ("idle_check_minutes", "active_check_minutes", "inactivity_timeout_minutes"):
        if not isinstance(config[key], int) or isinstance(config[key], bool) or config[key] < 1:
            raise CampaignError(f"{key} должен быть положительным целым числом")
    waits = config["short_wait_seconds"]
    if (
        not isinstance(waits, list)
        or not waits
        or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in waits)
    ):
        raise CampaignError("short_wait_seconds должен быть непустым списком целых секунд")
    probes = config["post_campaign_probe_minutes"]
    if (
        not isinstance(probes, list)
        or not probes
        or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in probes)
    ):
        raise CampaignError(
            "post_campaign_probe_minutes должен быть непустым списком целых минут"
        )
    config["post_campaign_probe_minutes"] = sorted(set(probes))
    return config


def message_key(message_id: str | None, fingerprint: str | None) -> str | None:
    if message_id and message_id.strip():
        return "id:" + normalize_key(message_id)
    if fingerprint and fingerprint.strip():
        return "fp:" + normalize_key(fingerprint)
    return None


def _chat_activity_base(task: dict[str, Any]) -> datetime:
    """Return a stable activity base; updated_at is intentionally not used."""
    for field in ("last_activity_at", "created_at"):
        if task.get(field):
            return parse_time(str(task[field]))
    raise CampaignError(f"У bot_chat {task.get('id')} нет created_at")


def ensure_chat_lifecycle(task: dict[str, Any]) -> None:
    """Normalize legacy chat state without treating polls as activity."""
    if task.get("type") != "bot_chat":
        return
    base = _chat_activity_base(task)
    task.setdefault("last_activity_at", iso(base))
    task.setdefault("idle_deadline_at", iso(base + timedelta(seconds=IDLE_TIMEOUT_SECONDS)))
    task.setdefault("seen_incoming_keys", [])
    task.setdefault("sent_response_keys", [])


def record_chat_activity(
    task: dict[str, Any],
    timestamp: datetime,
    *,
    incoming_key: str | None = None,
    response_key: str | None = None,
    inactivity_timeout_seconds: int = IDLE_TIMEOUT_SECONDS,
) -> bool:
    """Record a de-duplicated meaningful event and reset the one-hour deadline."""
    ensure_chat_lifecycle(task)
    meaningful = False
    if incoming_key and incoming_key not in task["seen_incoming_keys"]:
        task["seen_incoming_keys"].append(incoming_key)
        task["last_incoming_key"] = incoming_key
        meaningful = True
    if response_key and response_key not in task["sent_response_keys"]:
        task["sent_response_keys"].append(response_key)
        task["last_response_key"] = response_key
        meaningful = True
    if meaningful:
        task["last_activity_at"] = iso(timestamp)
        task["idle_deadline_at"] = iso(
            timestamp + timedelta(seconds=inactivity_timeout_seconds)
        )
    return meaningful


def expire_chat(task: dict[str, Any], timestamp: datetime) -> bool:
    """Persist the operational idle stop when the stable deadline is reached."""
    ensure_chat_lifecycle(task)
    if parse_time(str(task["idle_deadline_at"])) > timestamp:
        return False
    task["status"] = "dormant"
    task["outcome_reason"] = "idle_timeout"
    task["finished_at"] = iso(timestamp)
    task["updated_at"] = iso(timestamp)
    for field in ("next_check_at", "worker_id", "claim_token", "lease_expires_at"):
        task.pop(field, None)
    return True


def empty_state(coordinator_id: str | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "coordinator_id": coordinator_id,
        "updated_at": None,
        "tasks": [],
        "post_campaign_probes": [],
        "probe_attempts": [],
    }


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CampaignError(f"Некорректный JSON {path}: {exc}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("tasks"), list):
        raise CampaignError("Состояние кампании должно быть объектом со списком tasks")
    for field in ("post_campaign_probes", "probe_attempts"):
        if field in state and not isinstance(state[field], list):
            raise CampaignError(f"Поле {field} должно быть списком")
        state.setdefault(field, [])
    return state


def write_state_atomic(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


class FileLock:
    def __init__(self, path: Path, timeout: float = 5.0) -> None:
        self.path = path
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode("ascii"))
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise CampaignError(f"Не удалось получить блокировку {self.path.name}")
                time.sleep(0.05)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)


def require_coordinator(state: dict[str, Any], coordinator_id: str) -> None:
    if not coordinator_id.strip():
        raise CampaignError("coordinator_id не может быть пустым")
    known = state.get("coordinator_id")
    if known is None:
        state["coordinator_id"] = coordinator_id
    elif known != coordinator_id:
        raise CampaignError("Состояние принадлежит другому координатору")


def mutate(
    path: Path,
    coordinator_id: str,
    timestamp: datetime,
    operation: Callable[[dict[str, Any]], Any],
) -> Any:
    with FileLock(path.with_suffix(path.suffix + ".lock")):
        state = load_state(path)
        require_coordinator(state, coordinator_id)
        result = operation(state)
        state["updated_at"] = iso(timestamp)
        write_state_atomic(path, state)
        return result


def find_task(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in state["tasks"]:
        if task.get("id") == task_id:
            return task
    raise CampaignError(f"Задача не найдена: {task_id}")


def add_task(
    state: dict[str, Any],
    task_type: str,
    platform: str,
    timestamp: datetime,
    day: str | None = None,
    canonical_key: str | None = None,
    external_ref: str | None = None,
    inactivity_timeout_seconds: int = IDLE_TIMEOUT_SECONDS,
) -> tuple[str, dict[str, Any]]:
    platform = normalize_platform(platform)
    if task_type not in TASK_TYPES:
        raise CampaignError(f"Неизвестный тип задачи: {task_type}")
    if task_type in RISKY_TASK_TYPES and (
        canonical_key is None or not canonical_key.strip()
    ):
        raise CampaignError(
            f"canonical_key обязателен и не может быть пустым для {task_type}"
        )
    task_day = day or timestamp.date().isoformat()
    date.fromisoformat(task_day)
    normalized_key = normalize_key(canonical_key) if canonical_key else None
    if normalized_key:
        for existing in state["tasks"]:
            same_identity = (
                existing.get("type") == task_type
                and existing.get("canonical_key") == normalized_key
            )
            same_scan_day = (
                task_type != "source_scan" or existing.get("day") == task_day
            )
            if same_identity and same_scan_day:
                return "exists", existing
    task = {
        "id": f"task-{timestamp.strftime('%Y%m%d')}-{uuid.uuid4().hex[:10]}",
        "type": task_type,
        "day": task_day,
        "platform": platform,
        "canonical_key": normalized_key,
        "external_ref": external_ref,
        "status": "pending",
        "created_at": iso(timestamp),
        "updated_at": iso(timestamp),
        "attempt": 0,
    }
    if task_type == "bot_chat":
        task["last_activity_at"] = iso(timestamp)
        task["idle_deadline_at"] = iso(
            timestamp + timedelta(seconds=inactivity_timeout_seconds)
        )
        task["seen_incoming_keys"] = []
        task["sent_response_keys"] = []
    state["tasks"].append(task)
    return "created", task


def recover_stale(state: dict[str, Any], timestamp: datetime) -> list[str]:
    """Recover scans, but quarantine stale actions whose send state is unknown."""
    uncertain: list[str] = []
    for task in state["tasks"]:
        if task.get("status") != "running" or not task.get("lease_expires_at"):
            continue
        if parse_time(str(task["lease_expires_at"])) > timestamp:
            continue
        task["updated_at"] = iso(timestamp)
        task["stale_at"] = iso(timestamp)
        task.pop("lease_expires_at", None)
        if task.get("type") in RISKY_TASK_TYPES:
            task["status"] = "uncertain"
            task["needs_reconciliation"] = True
            uncertain.append(str(task["id"]))
        else:
            task["status"] = "pending"
            task.pop("worker_id", None)
            task.pop("claim_token", None)
    return uncertain


def claim_task(
    state: dict[str, Any],
    worker_id: str,
    timestamp: datetime,
    task_type: str | None = None,
    lease_seconds: int = 900,
    task_id: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if task_type is not None and task_type not in TASK_TYPES:
        raise CampaignError(f"Неизвестный тип задачи: {task_type}")
    if lease_seconds < 1:
        raise CampaignError("lease_seconds должен быть положительным")
    uncertain = recover_stale(state, timestamp)
    today = timestamp.date().isoformat()
    if task_id:
        requested = find_task(state, task_id)
        if requested.get("status") != "pending":
            raise CampaignError(f"Задача {task_id} не pending: {requested.get('status')}")
        if requested.get("type") != "bot_chat" and requested.get("day") != today:
            raise CampaignError(f"Задача {task_id} относится не к текущему локальному дню")
        if task_type is not None and requested.get("type") != task_type:
            raise CampaignError(f"Задача {task_id} имеет другой тип")
        candidates = [requested]
    else:
        candidates = [
            task
            for task in state["tasks"]
            if task.get("status") == "pending"
            and (task.get("type") == "bot_chat" or task.get("day") == today)
            and (task_type is None or task.get("type") == task_type)
        ]
    if not candidates:
        return None, uncertain
    task = candidates[0]
    task["status"] = "running"
    task["worker_id"] = worker_id
    task["claim_token"] = uuid.uuid4().hex
    task["lease_expires_at"] = iso(timestamp + timedelta(seconds=lease_seconds))
    task["attempt"] = int(task.get("attempt", 0)) + 1
    task["updated_at"] = iso(timestamp)
    return task, uncertain


def finish_task(
    state: dict[str, Any],
    task_id: str,
    timestamp: datetime,
    outcome: str,
    claim_token: str | None = None,
    artifact_ref: str | None = None,
    reconciled: bool = False,
    explicit_completion: bool = False,
) -> dict[str, Any]:
    if outcome not in {"completed", "failed", "retry", "dormant", "needs_user"}:
        raise CampaignError("outcome: completed, failed, retry, dormant или needs_user")
    task = find_task(state, task_id)
    status = task.get("status")
    if status == "running":
        if not claim_token or claim_token != task.get("claim_token"):
            raise CampaignError("Неверный или отсутствующий claim token")
        if outcome == "retry":
            raise CampaignError("Обычный running task нельзя вернуть без результата")
    elif status == "uncertain":
        if not reconciled:
            raise CampaignError("Необходима явная сверка: добавьте --reconciled")
    else:
        raise CampaignError(f"Задачу в статусе {status} нельзя завершить")

    if outcome == "dormant" and task.get("type") != "bot_chat":
        raise CampaignError("dormant применим только к bot_chat")
    if outcome == "needs_user" and task.get("type") != "bot_chat":
        raise CampaignError("needs_user применим только к bot_chat")
    if task.get("type") == "bot_chat" and outcome == "completed" and not explicit_completion:
        raise CampaignError(
            "completed для bot_chat требует явного завершения от бота: "
            "добавьте --explicit-completion"
        )
    if task.get("type") == "bot_chat" and outcome == "dormant":
        ensure_chat_lifecycle(task)
        if parse_time(str(task["idle_deadline_at"])) > timestamp:
            raise CampaignError("dormant допустим только после idle_deadline_at")

    task["updated_at"] = iso(timestamp)
    task.pop("lease_expires_at", None)
    if status == "uncertain":
        task["reconciled_at"] = iso(timestamp)
        task["needs_reconciliation"] = False
    if outcome == "retry":
        task["status"] = "pending"
        task.pop("worker_id", None)
        task.pop("claim_token", None)
    else:
        task["status"] = outcome
        task["finished_at"] = iso(timestamp)
        if outcome == "completed" and task.get("type") == "bot_chat":
            task["outcome_reason"] = "explicit_bot_completion"
        elif outcome == "dormant":
            task["outcome_reason"] = "idle_timeout"
        if artifact_ref:
            task["artifact_ref"] = artifact_ref
    return task


def renew_lease(
    state: dict[str, Any],
    task_id: str,
    timestamp: datetime,
    claim_token: str,
    worker_id: str,
    lease_seconds: int = 900,
) -> dict[str, Any]:
    """Extend an unexpired claim; an expired risky action must be reconciled."""
    if lease_seconds < 1:
        raise CampaignError("lease_seconds должен быть положительным")
    task = find_task(state, task_id)
    if task.get("status") != "running":
        raise CampaignError("Продлить можно только running-задачу")
    if task.get("claim_token") != claim_token or task.get("worker_id") != worker_id:
        raise CampaignError("Неверные worker_id или claim token")
    if not task.get("lease_expires_at") or parse_time(str(task["lease_expires_at"])) <= timestamp:
        raise CampaignError("Lease уже истёк; risky-задача требует явной сверки")
    task["lease_expires_at"] = iso(timestamp + timedelta(seconds=lease_seconds))
    task["updated_at"] = iso(timestamp)
    return task


def _require_local_artifact_ref(artifact_ref: str) -> str:
    """Accept only a nonempty evidence reference below this workspace's .local."""
    reference = artifact_ref.strip()
    if not reference:
        raise CampaignError("Handoff требует непустой --artifact-ref")
    candidate = Path(reference)
    local_root = local_path("").resolve()
    try:
        if candidate.is_absolute():
            resolved = candidate.resolve()
        elif candidate.parts and candidate.parts[0] == ".local":
            resolved = (local_root.parent / candidate).resolve()
        else:
            resolved = (local_root / candidate).resolve()
            reference = (Path(".local") / candidate).as_posix()
        if not resolved.is_relative_to(local_root):
            raise ValueError("outside local root")
    except ValueError as exc:
        raise CampaignError("artifact-ref для handoff должен оставаться внутри .local") from exc
    return reference


def handoff_task(
    state: dict[str, Any],
    task_id: str,
    timestamp: datetime,
    claim_token: str,
    worker_id: str,
    new_worker_id: str,
    artifact_ref: str,
    reconciled: bool,
    lease_seconds: int = MAX_HANDOFF_LEASE_SECONDS,
) -> dict[str, Any]:
    """Transfer one live claim after reconciliation, without repeating its action."""
    if not reconciled:
        raise CampaignError("Handoff требует явную сверку: добавьте --reconciled")
    artifact_ref = _require_local_artifact_ref(artifact_ref)
    if not worker_id.strip() or not new_worker_id.strip():
        raise CampaignError("worker-id и new-worker-id не могут быть пустыми")
    if worker_id == new_worker_id:
        raise CampaignError("Handoff требует нового worker-id")
    if not 1 <= lease_seconds <= MAX_HANDOFF_LEASE_SECONDS:
        raise CampaignError(
            f"lease_seconds для handoff должен быть от 1 до {MAX_HANDOFF_LEASE_SECONDS}"
        )
    task = find_task(state, task_id)
    if task.get("status") != "running":
        raise CampaignError("Handoff возможен только для running-задачи")
    if task.get("claim_token") != claim_token or task.get("worker_id") != worker_id:
        raise CampaignError("Неверные worker_id или claim token")
    if not task.get("lease_expires_at") or parse_time(str(task["lease_expires_at"])) <= timestamp:
        raise CampaignError("Истёкший lease нельзя передать: сначала сверить uncertain-задачу")
    if any(
        other is not task
        and other.get("status") == "running"
        and other.get("worker_id") == new_worker_id
        for other in state["tasks"]
    ):
        raise CampaignError("new-worker-id уже владеет другой running-задачей")

    task.setdefault("handoff_history", []).append({
        "at": iso(timestamp),
        "previous_worker_id": worker_id,
        "artifact_ref": artifact_ref,
    })
    task["worker_id"] = new_worker_id
    task["claim_token"] = uuid.uuid4().hex
    task["lease_expires_at"] = iso(timestamp + timedelta(seconds=lease_seconds))
    task["handoff_reconciled_at"] = iso(timestamp)
    task["updated_at"] = iso(timestamp)
    task["attempt"] = int(task.get("attempt", 0)) + 1
    return task


def defer_chat(
    state: dict[str, Any],
    task_id: str,
    timestamp: datetime,
    claim_token: str,
    incoming: bool = False,
    morning_hour: int = 10,
    incoming_message_id: str | None = None,
    incoming_fingerprint: str | None = None,
    response_sent: bool = False,
    response_message_id: str | None = None,
    response_fingerprint: str | None = None,
    closing: bool = False,
    active_poll_seconds: int = ACTIVE_POLL_SECONDS,
    inactivity_timeout_seconds: int = IDLE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    task = find_task(state, task_id)
    if task.get("type") != "bot_chat":
        raise CampaignError("defer-chat применим только к bot_chat")
    if task.get("status") != "running" or task.get("claim_token") != claim_token:
        raise CampaignError("Чат должен быть running с корректным claim token")
    del morning_hour  # retained in the API for callers upgrading from the old queue
    incoming_key = message_key(incoming_message_id, incoming_fingerprint)
    response_key = message_key(response_message_id, response_fingerprint)
    if incoming and not incoming_key:
        raise CampaignError("Новое входящее требует message ID или fingerprint")
    if response_sent and not response_key:
        raise CampaignError("Отправленный ответ требует message ID или fingerprint")
    if closing and not response_sent:
        raise CampaignError("closing допустим только вместе с реально отправленным ответом")
    meaningful = record_chat_activity(
        task,
        timestamp,
        incoming_key=incoming_key if incoming else None,
        response_key=response_key if response_sent else None,
        inactivity_timeout_seconds=inactivity_timeout_seconds,
    )
    task["persistent_wait_count"] = int(task.get("persistent_wait_count", 0)) + 1
    task["updated_at"] = iso(timestamp)
    task.pop("lease_expires_at", None)
    task.pop("worker_id", None)
    task.pop("claim_token", None)
    if closing:
        task["closing_at"] = iso(timestamp)
        task["closing"] = True
    if expire_chat(task, timestamp):
        return task
    task["status"] = "waiting"
    task["next_check_at"] = iso(timestamp + timedelta(seconds=active_poll_seconds))
    task["last_poll_meaningful"] = meaningful
    return task


def activate_due(
    state: dict[str, Any],
    timestamp: datetime,
    morning_scan: bool = False,
    morning_hour: int = 10,
) -> list[dict[str, Any]]:
    """Persist expirations and activate due waits without a daily reset."""
    del morning_scan, morning_hour  # compatibility only; midnight has no special meaning
    activated: list[dict[str, Any]] = []
    for task in state["tasks"]:
        if task.get("type") != "bot_chat" or task.get("status") not in {
            "pending", "waiting", "deferred"
        }:
            continue
        if expire_chat(task, timestamp):
            continue
        if task.get("status") == "pending":
            continue
        if not task.get("next_check_at") or parse_time(str(task["next_check_at"])) > timestamp:
            continue
        task["status"] = "pending"
        task["day"] = timestamp.date().isoformat()
        task["updated_at"] = iso(timestamp)
        task.pop("next_check_at", None)
        activated.append(task)
    return activated


def resume_chat(
    state: dict[str, Any],
    task_id: str,
    timestamp: datetime,
    incoming: bool,
    message_id: str | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Reopen a terminal bot chat only after a newly observed incoming message."""
    if not incoming:
        raise CampaignError("resume-chat требует подтверждения --incoming")
    task = find_task(state, task_id)
    if task.get("type") != "bot_chat":
        raise CampaignError("resume-chat применим только к bot_chat")
    incoming_key = message_key(message_id, fingerprint)
    if not incoming_key:
        raise CampaignError("Новое входящее требует message ID или fingerprint")
    ensure_chat_lifecycle(task)
    if incoming_key in task["seen_incoming_keys"]:
        task["resume_duplicate"] = True
        return task
    if task.get("status") not in {"completed", "dormant", "deferred"}:
        raise CampaignError("Возобновить можно только completed, dormant или deferred bot_chat")
    previous_status = str(task["status"])
    record_chat_activity(task, timestamp, incoming_key=incoming_key)
    task["status"] = "pending"
    task["day"] = timestamp.date().isoformat()
    task["updated_at"] = iso(timestamp)
    task["reopened_at"] = iso(timestamp)
    task["reopened_from"] = previous_status
    task.pop("resume_duplicate", None)
    task["incoming_count"] = int(task.get("incoming_count", 0)) + 1
    task["persistent_wait_count"] = 0
    for field in ("finished_at", "next_check_at", "worker_id", "claim_token", "lease_expires_at"):
        task.pop(field, None)
    return task


def schedule_post_campaign(
    state: dict[str, Any],
    campaign_key: str,
    completed_at: datetime,
    timestamp: datetime,
    probe_minutes: list[int] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Create one durable, non-resetting inbox probe series per campaign."""
    normalized_key = normalize_key(campaign_key)
    if not normalized_key:
        raise CampaignError("campaign_key не может быть пустым")
    if completed_at > timestamp:
        raise CampaignError("completed_at не может быть в будущем")
    batches = state.setdefault("post_campaign_probes", [])
    for existing in batches:
        if existing.get("campaign_key") == normalized_key:
            return "exists", existing
    offsets = sorted(set(probe_minutes or POST_CAMPAIGN_PROBE_MINUTES))
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in offsets):
        raise CampaignError("probe_minutes должны быть положительными целыми минутами")
    batch = {
        "id": f"campaign-{uuid.uuid4().hex[:12]}",
        "campaign_key": normalized_key,
        "completed_at": iso(completed_at),
        "status": "scheduled",
        "created_at": iso(timestamp),
        "updated_at": iso(timestamp),
        "probes": [
            {
                "offset_minutes": offset,
                "due_at": iso(completed_at + timedelta(minutes=offset)),
                "status": "pending",
            }
            for offset in offsets
        ],
    }
    batches.append(batch)
    return "created", batch


def due_post_campaign_probes(
    state: dict[str, Any], timestamp: datetime
) -> dict[str, Any]:
    """Describe all currently due probes; one inbox scan satisfies the set."""
    campaigns: list[dict[str, Any]] = []
    total = 0
    for batch in state.get("post_campaign_probes", []):
        due = [
            probe for probe in batch.get("probes", [])
            if probe.get("status") == "pending"
            and parse_time(str(probe["due_at"])) <= timestamp
        ]
        if not due:
            continue
        total += len(due)
        campaigns.append({
            "campaign_key": batch["campaign_key"],
            "campaign_id": batch["id"],
            "completed_at": batch["completed_at"],
            "due_probe_minutes": [probe["offset_minutes"] for probe in due],
            "due_at": [probe["due_at"] for probe in due],
        })
    return {
        "status": "due" if total else "empty",
        "scan_required": bool(total),
        "action": "read_inboxes_only" if total else None,
        "checked_at": iso(timestamp),
        "due_probe_count": total,
        "campaigns": campaigns,
    }


def acknowledge_post_campaign_probes(
    state: dict[str, Any],
    timestamp: datetime,
    outcome: str,
    attempt_key: str,
    *,
    campaign_key: str | None = None,
    artifact_ref: str | None = None,
    error_code: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Terminally record one inbox scan for every selected probe already due."""
    if outcome not in {"success", "error"}:
        raise CampaignError("outcome: success или error")
    normalized_attempt = normalize_key(attempt_key)
    if not normalized_attempt:
        raise CampaignError("attempt_key не может быть пустым")
    if outcome == "error" and (error_code is None or not error_code.strip()):
        raise CampaignError("error outcome требует --error-code")
    attempts = state.setdefault("probe_attempts", [])
    for existing in attempts:
        if existing.get("attempt_key") == normalized_attempt:
            return "exists", existing

    normalized_campaign = normalize_key(campaign_key) if campaign_key else None
    if campaign_key is not None and not normalized_campaign:
        raise CampaignError("campaign_key не может быть пустым")
    matched_campaign = normalized_campaign is None
    satisfied: list[dict[str, Any]] = []
    for batch in state.get("post_campaign_probes", []):
        if normalized_campaign and batch.get("campaign_key") != normalized_campaign:
            continue
        matched_campaign = True
        due = [
            probe for probe in batch.get("probes", [])
            if probe.get("status") == "pending"
            and parse_time(str(probe["due_at"])) <= timestamp
        ]
        if not due:
            continue
        for probe in due:
            probe["status"] = outcome
            probe["attempted_at"] = iso(timestamp)
            probe["attempt_key"] = normalized_attempt
            if outcome == "error":
                probe["error_code"] = error_code.strip()
        batch["updated_at"] = iso(timestamp)
        pending = [probe for probe in batch["probes"] if probe["status"] == "pending"]
        if not pending:
            batch["status"] = "finished"
            batch["finished_at"] = iso(timestamp)
        satisfied.append({
            "campaign_key": batch["campaign_key"],
            "probe_minutes": [probe["offset_minutes"] for probe in due],
        })
    if normalized_campaign and not matched_campaign:
        raise CampaignError(f"Кампания не найдена: {campaign_key}")
    if not satisfied:
        return "no_due", {
            "attempt_key": normalized_attempt,
            "attempted_at": iso(timestamp),
            "outcome": outcome,
            "satisfied_probe_count": 0,
            "satisfied": [],
        }
    attempt = {
        "attempt_key": normalized_attempt,
        "attempted_at": iso(timestamp),
        "outcome": outcome,
        "satisfied_probe_count": sum(
            len(item["probe_minutes"]) for item in satisfied
        ),
        "satisfied": satisfied,
    }
    if artifact_ref:
        attempt["artifact_ref"] = artifact_ref
    if outcome == "error":
        attempt["error_code"] = error_code.strip()
    attempts.append(attempt)
    return "recorded", attempt


def _next_pending_probe_at(state: dict[str, Any]) -> datetime | None:
    pending = [
        parse_time(str(probe["due_at"]))
        for batch in state.get("post_campaign_probes", [])
        for probe in batch.get("probes", [])
        if probe.get("status") == "pending"
    ]
    return min(pending) if pending else None


def schedule_mode(
    state: dict[str, Any],
    timestamp: datetime,
    *,
    active_check_minutes: int = ACTIVE_POLL_SECONDS // 60,
    idle_check_minutes: int = IDLE_POLL_SECONDS // 60,
    inactivity_timeout_minutes: int = IDLE_TIMEOUT_SECONDS // 60,
    short_wait_seconds: list[int] | None = None,
    post_campaign_probe_minutes: list[int] | None = None,
) -> dict[str, Any]:
    """Return a read-only plan for the app scheduler (never a worker concern)."""
    active_ids: list[str] = []
    by_status: dict[str, int] = {}
    for task in state["tasks"]:
        if task.get("type") != "bot_chat" or task.get("status") not in ACTIVE_CHAT_STATUSES:
            continue
        if task.get("status") in {"pending", "waiting"}:
            if task.get("idle_deadline_at"):
                deadline = parse_time(str(task["idle_deadline_at"]))
            else:
                deadline = _chat_activity_base(task) + timedelta(
                    minutes=inactivity_timeout_minutes
                )
            if deadline <= timestamp:
                continue
        active_ids.append(str(task.get("id")))
        status = str(task.get("status"))
        by_status[status] = by_status.get(status, 0) + 1
    chat_check_at = (
        timestamp + timedelta(minutes=active_check_minutes) if active_ids else None
    )
    probe_check_at = _next_pending_probe_at(state)
    if probe_check_at is not None and (
        chat_check_at is None or probe_check_at <= chat_check_at
    ):
        mode = "post_campaign_probe"
        next_run_at = max(timestamp, probe_check_at)
    elif chat_check_at is not None:
        mode = "active"
        next_run_at = chat_check_at
    else:
        mode = "idle"
        next_run_at = timestamp + timedelta(minutes=idle_check_minutes)
    interval_minutes = max(
        0, math.ceil((next_run_at - timestamp).total_seconds() / 60)
    )
    return {
        "mode": mode,
        "interval_minutes": interval_minutes,
        "next_run_at": iso(next_run_at),
        "run_now": next_run_at <= timestamp,
        "timing": {
            "idle_check_minutes": idle_check_minutes,
            "active_check_minutes": active_check_minutes,
            "inactivity_timeout_minutes": inactivity_timeout_minutes,
            "short_wait_seconds": short_wait_seconds or [10, 30, 60],
            "post_campaign_probe_minutes": (
                post_campaign_probe_minutes or POST_CAMPAIGN_PROBE_MINUTES
            ),
        },
        "active_chat_count": len(active_ids),
        "active_chat_ids": active_ids,
        "active_by_status": by_status,
        "next_pending_probe_at": iso(probe_check_at) if probe_check_at else None,
    }


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def command_add(args: argparse.Namespace) -> int:
    timestamp = parse_time(args.now)
    monitor = load_monitor_config(args.monitor_config)

    def operation(state: dict[str, Any]) -> dict[str, Any]:
        status, task = add_task(
            state, args.task_type, args.platform, timestamp, args.day,
            args.canonical_key, args.external_ref,
            int(monitor["inactivity_timeout_minutes"]) * 60,
        )
        return {"status": status, "task": task}

    print_json(mutate(args.state, args.coordinator_id, timestamp, operation))
    return 0


def command_claim(args: argparse.Namespace) -> int:
    timestamp = parse_time(args.now)

    def operation(state: dict[str, Any]) -> dict[str, Any]:
        task, uncertain = claim_task(
            state, args.worker_id, timestamp, args.task_type, args.lease_seconds,
            args.task_id,
        )
        return {"status": "claimed" if task else "empty", "task": task, "uncertain": uncertain}

    print_json(mutate(args.state, args.coordinator_id, timestamp, operation))
    return 0


def command_finish(args: argparse.Namespace) -> int:
    timestamp = parse_time(args.now)
    result = mutate(
        args.state,
        args.coordinator_id,
        timestamp,
        lambda state: finish_task(
            state, args.task_id, timestamp, args.outcome, args.claim_token,
            args.artifact_ref, args.reconciled, args.explicit_completion,
        ),
    )
    print_json({"status": result["status"], "task": result})
    return 0


def command_defer_chat(args: argparse.Namespace) -> int:
    timestamp = parse_time(args.now)
    monitor = load_monitor_config(args.monitor_config)
    result = mutate(
        args.state,
        args.coordinator_id,
        timestamp,
        lambda state: defer_chat(
            state, args.task_id, timestamp, args.claim_token, args.incoming,
            args.morning_hour, args.incoming_message_id,
            args.incoming_fingerprint, args.response_sent,
            args.response_message_id, args.response_fingerprint, args.closing,
            int(monitor["active_check_minutes"]) * 60,
            int(monitor["inactivity_timeout_minutes"]) * 60,
        ),
    )
    print_json({"status": result["status"], "task": result})
    return 0


def command_due(args: argparse.Namespace) -> int:
    timestamp = parse_time(args.now)
    tasks = mutate(
        args.state,
        args.coordinator_id,
        timestamp,
        lambda state: activate_due(
            state, timestamp, args.morning_scan, args.morning_hour
        ),
    )
    print_json({"status": "activated", "count": len(tasks), "tasks": tasks})
    return 0


def command_resume_chat(args: argparse.Namespace) -> int:
    timestamp = parse_time(args.now)
    task = mutate(
        args.state,
        args.coordinator_id,
        timestamp,
        lambda state: resume_chat(
            state, args.task_id, timestamp, args.incoming,
            args.message_id, args.fingerprint,
        ),
    )
    print_json({"status": task["status"], "task": task})
    return 0


def command_renew(args: argparse.Namespace) -> int:
    timestamp = parse_time(args.now)
    task = mutate(
        args.state,
        args.coordinator_id,
        timestamp,
        lambda state: renew_lease(
            state, args.task_id, timestamp, args.claim_token,
            args.worker_id, args.lease_seconds,
        ),
    )
    print_json({"status": "renewed", "task": task})
    return 0


def command_handoff(args: argparse.Namespace) -> int:
    timestamp = now_local()
    task = mutate(
        args.state,
        args.coordinator_id,
        timestamp,
        lambda state: handoff_task(
            state, args.task_id, timestamp, args.claim_token, args.worker_id,
            args.new_worker_id, args.artifact_ref, args.reconciled, args.lease_seconds,
        ),
    )
    print_json({"status": "handed_off", "task": task})
    return 0


def command_schedule_mode(args: argparse.Namespace) -> int:
    timestamp = parse_time(args.now)
    monitor = load_monitor_config(args.monitor_config)
    from hr_monitor_guard import apply_schedule, status as guard_status
    print_json(apply_schedule(schedule_mode(load_state(args.state), timestamp, **monitor),
                              guard_status(now=timestamp), timestamp))
    return 0


def command_post_campaign(args: argparse.Namespace) -> int:
    timestamp = parse_time(args.now)
    completed_at = parse_time(args.completed_at) if args.completed_at else timestamp
    monitor = load_monitor_config(args.monitor_config)

    def operation(state: dict[str, Any]) -> dict[str, Any]:
        status, batch = schedule_post_campaign(
            state,
            args.campaign_key,
            completed_at,
            timestamp,
            list(monitor["post_campaign_probe_minutes"]),
        )
        return {"status": status, "batch": batch}

    print_json(mutate(args.state, args.coordinator_id, timestamp, operation))
    return 0


def command_due_probes(args: argparse.Namespace) -> int:
    timestamp = parse_time(args.now)
    print_json(due_post_campaign_probes(load_state(args.state), timestamp))
    return 0


def command_ack_probe(args: argparse.Namespace) -> int:
    timestamp = parse_time(args.now)

    def operation(state: dict[str, Any]) -> dict[str, Any]:
        status, attempt = acknowledge_post_campaign_probes(
            state,
            timestamp,
            args.outcome,
            args.attempt_key,
            campaign_key=args.campaign_key,
            artifact_ref=args.artifact_ref,
            error_code=args.error_code,
        )
        return {"status": status, "attempt": attempt}

    print_json(mutate(args.state, args.coordinator_id, timestamp, operation))
    return 0


def command_list(args: argparse.Namespace) -> int:
    timestamp = parse_time(args.now)
    state = load_state(args.state)
    selected_day = args.day or timestamp.date().isoformat()
    tasks = [
        task for task in state["tasks"]
        if (args.all_days or task.get("day") == selected_day)
        and (args.status is None or task.get("status") == args.status)
        and (args.task_type is None or task.get("type") == args.task_type)
    ]
    print_json({"day": None if args.all_days else selected_day, "count": len(tasks), "tasks": tasks})
    return 0


def add_common_mutation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--coordinator-id", default="hr-main",
        help="Постоянный ID координатора (по умолчанию hr-main)",
    )
    parser.add_argument("--now", help="ISO timestamp; предназначен также для воспроизводимых запусков")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Координаторская очередь ежедневной HR-кампании",
        epilog=(
            "Workers не изменяют JSON и не запускают mutating-команды: координатор "
            "передаёт им task/claim_token и затем записывает ссылку на артефакт."
        ),
    )
    parser.add_argument("--state", type=Path, default=STATE_PATH, help="Путь к campaign-state.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Добавить задачу; canonical key идемпотентен между площадками")
    add_common_mutation_args(add_parser)
    add_parser.add_argument("--type", dest="task_type", choices=sorted(TASK_TYPES), required=True)
    add_parser.add_argument("--platform", required=True)
    add_parser.add_argument("--day", help="YYYY-MM-DD; по умолчанию день в часовом поясе владельца")
    add_parser.add_argument(
        "--canonical-key",
        help=(
            "Канонический ID вакансии/диалога без названия площадки; "
            "обязателен для application и bot_chat"
        ),
    )
    add_parser.add_argument("--external-ref", help="Несекретная ссылка или локальный ID исходных данных")
    add_parser.add_argument("--monitor-config", type=Path, default=MONITOR_CONFIG_PATH)
    add_parser.set_defaults(handler=command_add)

    claim_parser = subparsers.add_parser("claim", help="Назначить одну pending-задачу worker")
    add_common_mutation_args(claim_parser)
    claim_parser.add_argument("--worker-id", required=True)
    claim_parser.add_argument("--task-id", help="Назначить конкретную pending-задачу")
    claim_parser.add_argument("--type", dest="task_type", choices=sorted(TASK_TYPES))
    claim_parser.add_argument("--lease-seconds", type=int, default=900)
    claim_parser.set_defaults(handler=command_claim)

    finish_parser = subparsers.add_parser("finish", help="Записать результат worker или сверить uncertain-задачу")
    add_common_mutation_args(finish_parser)
    finish_parser.add_argument("--task-id", required=True)
    finish_parser.add_argument("--claim-token")
    finish_parser.add_argument(
        "--outcome", choices=("completed", "failed", "retry", "dormant", "needs_user"),
        required=True,
    )
    finish_parser.add_argument("--artifact-ref", help="Ссылка/путь к артефакту, не его секретное содержимое")
    finish_parser.add_argument("--reconciled", action="store_true", help="Подтверждает ручную сверку uncertain-задачи")
    finish_parser.add_argument(
        "--explicit-completion", action="store_true",
        help="Для bot_chat: бот явно сообщил о завершении сценария",
    )
    finish_parser.set_defaults(handler=command_finish)

    renew_parser = subparsers.add_parser(
        "renew-lease", aliases=["renew"],
        help="Продлить ещё не истёкший worker lease",
    )
    add_common_mutation_args(renew_parser)
    renew_parser.add_argument("--task-id", required=True)
    renew_parser.add_argument("--worker-id", required=True)
    renew_parser.add_argument("--claim-token", required=True)
    renew_parser.add_argument("--lease-seconds", type=int, default=900)
    renew_parser.set_defaults(handler=command_renew)

    handoff_parser = subparsers.add_parser(
        "handoff", help="Передать действующий claim другому idle worker после сверки"
    )
    handoff_parser.add_argument("--coordinator-id", default="hr-main")
    handoff_parser.add_argument("--task-id", required=True)
    handoff_parser.add_argument("--worker-id", required=True)
    handoff_parser.add_argument("--new-worker-id", required=True)
    handoff_parser.add_argument("--claim-token", required=True)
    handoff_parser.add_argument("--artifact-ref", required=True)
    handoff_parser.add_argument("--reconciled", action="store_true")
    handoff_parser.add_argument("--lease-seconds", type=int, default=MAX_HANDOFF_LEASE_SECONDS)
    handoff_parser.set_defaults(handler=command_handoff)

    list_parser = subparsers.add_parser("list", help="Показать задачи, включая финальные статусы текущего дня")
    list_parser.add_argument("--now")
    list_parser.add_argument("--day")
    list_parser.add_argument("--all-days", action="store_true")
    list_parser.add_argument("--status")
    list_parser.add_argument("--type", dest="task_type", choices=sorted(TASK_TYPES))
    list_parser.set_defaults(handler=command_list)

    defer_parser = subparsers.add_parser("defer-chat", help="Поставить bot chat на проверку через 5 минут")
    add_common_mutation_args(defer_parser)
    defer_parser.add_argument("--task-id", required=True)
    defer_parser.add_argument("--claim-token", required=True)
    defer_parser.add_argument("--incoming", action="store_true", help="Новое входящее: сбросить задержку обратно на 5 минут")
    defer_parser.add_argument("--incoming-message-id")
    defer_parser.add_argument("--incoming-fingerprint")
    defer_parser.add_argument("--response-sent", action="store_true")
    defer_parser.add_argument("--response-message-id")
    defer_parser.add_argument("--response-fingerprint")
    defer_parser.add_argument(
        "--closing", action="store_true",
        help="Отправлено финальное «других вопросов нет»; это не completed",
    )
    defer_parser.add_argument(
        "--morning-hour", type=int, default=10, help=argparse.SUPPRESS
    )
    defer_parser.add_argument("--monitor-config", type=Path, default=MONITOR_CONFIG_PATH)
    defer_parser.set_defaults(handler=command_defer_chat)

    due_parser = subparsers.add_parser(
        "due", help="Зафиксировать idle timeout и активировать созревшие bot chats"
    )
    add_common_mutation_args(due_parser)
    due_parser.add_argument(
        "--morning-scan", action="store_true",
        help=argparse.SUPPRESS,
    )
    due_parser.add_argument(
        "--morning-hour", type=int, default=10, help=argparse.SUPPRESS
    )
    due_parser.set_defaults(handler=command_due)

    resume_parser = subparsers.add_parser(
        "resume-chat",
        help="Возобновить completed/dormant/deferred чат после нового входящего",
    )
    add_common_mutation_args(resume_parser)
    resume_parser.add_argument("--task-id", required=True)
    resume_parser.add_argument(
        "--incoming", action="store_true", required=True,
        help="Подтверждает наблюдение нового входящего сообщения",
    )
    resume_parser.add_argument("--message-id")
    resume_parser.add_argument("--fingerprint")
    resume_parser.set_defaults(handler=command_resume_chat)

    post_campaign_parser = subparsers.add_parser(
        "post-campaign",
        help="После фактического завершения батча создать inbox probes",
    )
    add_common_mutation_args(post_campaign_parser)
    post_campaign_parser.add_argument("--campaign-key", required=True)
    post_campaign_parser.add_argument(
        "--completed-at",
        help="ISO-время фактического завершения; по умолчанию --now",
    )
    post_campaign_parser.add_argument(
        "--monitor-config", type=Path, default=MONITOR_CONFIG_PATH
    )
    post_campaign_parser.set_defaults(handler=command_post_campaign)

    due_probes_parser = subparsers.add_parser(
        "due-probes",
        help="Read-only: показать due inbox probes, не выполнять отправки",
    )
    due_probes_parser.add_argument("--now")
    due_probes_parser.set_defaults(handler=command_due_probes)

    ack_probe_parser = subparsers.add_parser(
        "ack-probe",
        help="Записать один завершённый inbox scan для всех due probes",
    )
    add_common_mutation_args(ack_probe_parser)
    ack_probe_parser.add_argument("--attempt-key", required=True)
    ack_probe_parser.add_argument("--campaign-key")
    ack_probe_parser.add_argument(
        "--outcome", choices=("success", "error"), required=True
    )
    ack_probe_parser.add_argument("--artifact-ref")
    ack_probe_parser.add_argument("--error-code")
    ack_probe_parser.set_defaults(handler=command_ack_probe)

    schedule_parser = subparsers.add_parser(
        "schedule-mode", help="Read-only план: chat 5, probe exact, idle 240 минут"
    )
    schedule_parser.add_argument("--now")
    schedule_parser.add_argument("--monitor-config", type=Path, default=MONITOR_CONFIG_PATH)
    schedule_parser.set_defaults(handler=command_schedule_mode)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if not args.state.resolve().is_relative_to(local_path("")):
            raise CampaignError("--state must stay inside .local")
        if getattr(args, "morning_hour", 10) not in range(24):
            raise CampaignError("morning-hour должен быть от 0 до 23")
        return int(args.handler(args))
    except (CampaignError, OSError, ValueError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
