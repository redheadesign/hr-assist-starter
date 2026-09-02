"""Paths and configuration. All candidate data stays below .local."""
import json
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"


def local_path(relative):
    base = LOCAL.resolve()
    # A redirected .local would defeat the documented privacy boundary.
    if base != ROOT.resolve() / ".local":
        raise ValueError(".local must not point outside this workspace")
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError("Output must stay inside .local")
    return path


def read_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def timezone_for_workspace():
    name = read_json(local_path("config/settings.json"), {}).get("timezone", "UTC")
    if name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Check the IANA timezone and install requirements.txt") from exc
