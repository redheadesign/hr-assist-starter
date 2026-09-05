#!/usr/bin/env python3
"""Deterministic checks for job application copy."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from hr_policy import normalize_platform, read_config


def _load_config() -> dict[str, object]:
    return read_config("channels.json")


CONFIG = _load_config()
CHANNELS = {
    name: {
        "target": tuple(settings["target_range_excluding_links"])
        if settings["target_range_excluding_links"] is not None
        else None,
        "maximum": settings["hard_max_chars_excluding_links"],
        "sentence_words": settings["sentence_word_warning"],
        "paragraph_chars": settings["max_short_paragraph_chars"],
    }
    for name, settings in CONFIG["channels"].items()
}
PLATFORMS = CONFIG["platforms"]
PLATFORM_ALIASES = {
    alias: canonical
    for canonical, settings in PLATFORMS.items()
    for alias in settings.get("aliases", [])
}
VALID_CHANNELS = tuple(dict.fromkeys((*CHANNELS, *PLATFORMS, *PLATFORM_ALIASES)))

SLOP_PHRASES = (
    "данная вакансия",
    "динамично развивающаяся компания",
    "готов к новым вызовам",
    "буду рад стать частью",
    "внести значимый вклад",
    "уникальная возможность",
    "идеально соответствует",
    "командный игрок",
    "осуществлял",
    "в рамках сотрудничества",
    "являюсь специалистом",
)

EDITOR_MARKERS = (
    "удалить обещание",
    "замени на",
    "вставить сюда",
    "комментарий для",
    "редакторская заметка",
    "копировать как есть:",
)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+(?:[-–][A-Za-zА-Яа-яЁё0-9]+)?")
SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?", re.MULTILINE)


def read_text(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    return sys.stdin.read()


def visible_length(text: str) -> int:
    without_urls = URL_RE.sub("", text)
    return len(re.sub(r"\s+", " ", without_urls).strip())


def issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def resolve_validation_profile(channel: str, *, employer_question: bool = False) -> str:
    """Resolve a profile name while keeping profile names valid as direct inputs."""
    if employer_question:
        return "form_answer"
    normalized_channel = channel.strip().casefold()
    if normalized_channel in CHANNELS:
        return normalized_channel
    canonical = normalize_platform(normalized_channel, CONFIG)
    return str(PLATFORMS[canonical]["short_application_profile"])


def validate(
    text: str,
    channel: str,
    *,
    employer_question: bool = False,
    maximum_chars: int | None = None,
    length_unit: str = "characters",
) -> dict[str, object]:
    if maximum_chars is not None and maximum_chars < 1:
        raise ValueError("maximum_chars must be a positive integer")

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    notes: list[dict[str, str]] = []
    normalized = text.strip()
    length = visible_length(text)
    profile = resolve_validation_profile(channel, employer_question=employer_question)
    rules = CHANNELS[profile]
    effective_maximum = maximum_chars if maximum_chars is not None else rules["maximum"]
    actual_length = len(text.encode("utf-16-le")) // 2 if length_unit == "utf16" else len(text)
    checked_length = actual_length if maximum_chars is not None else length

    if not normalized:
        errors.append(issue("empty", "Текст пуст."))
    if "\u2014" in text:
        errors.append(issue("em_dash", "Найдено длинное тире. Используй среднее тире или дефис."))
    if effective_maximum is not None and checked_length > int(effective_maximum):
        unit = "единиц поля со ссылками и переносами" if maximum_chars is not None else "знаков без ссылок"
        errors.append(issue("too_long", f"{checked_length} {unit}, максимум – {effective_maximum}."))

    lower = normalized.casefold()
    for marker in EDITOR_MARKERS:
        if marker in lower:
            errors.append(issue("editor_note", f"Похоже на внутренний комментарий: «{marker}»."))
    for phrase in SLOP_PHRASES:
        if phrase in lower:
            warnings.append(issue("generic_phrase", f"Проверь шаблонную формулу: «{phrase}»."))

    target = rules["target"]
    if target is not None and normalized:
        minimum, maximum = target
        if length < minimum:
            notes.append(issue("compact", f"{length} знаков без ссылок. Это короче обычного ориентира, но допустимо, если мысль завершена."))
        elif length > maximum:
            warnings.append(issue("above_recommended", f"{length} знаков без ссылок. Рекомендуемый верхний ориентир – {maximum}."))

    if profile == "headhunter" and "с уважением" in lower:
        warnings.append(issue("hh_signature", "На HH формальная подпись обычно не нужна."))

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    if profile in {"headhunter", "linkedin_dm", "telegram", "email"} and len(paragraphs) > 4:
        warnings.append(issue("many_paragraphs", f"Короткий формат содержит {len(paragraphs)} абзацев."))

    paragraph_limit = int(rules["paragraph_chars"])
    for paragraph in paragraphs:
        compact_paragraph = re.sub(r"\s+", " ", URL_RE.sub("", paragraph)).strip()
        if len(compact_paragraph) > paragraph_limit:
            warnings.append(issue("dense_paragraph", f"Абзац на {len(compact_paragraph)} знаков выглядит плотным. Ориентир – до {paragraph_limit}."))
            break

    for sentence in SENTENCE_RE.findall(normalized):
        words = WORD_RE.findall(sentence)
        sentence_limit = int(rules["sentence_words"])
        if len(words) > sentence_limit:
            warnings.append(issue("long_sentence", f"Предложение на {len(words)} слов трудно читать с телефона. Ориентир – до {sentence_limit}."))
            break

    links = URL_RE.findall(text)
    if len(links) > 3:
        warnings.append(issue("many_links", f"В тексте {len(links)} ссылки. Обычно достаточно одной."))

    return {
        "channel": channel,
        "characters_excluding_links": length,
        "actual_field_length": actual_length,
        "length_unit": length_unit,
        "errors": errors,
        "warnings": warnings,
        "notes": notes,
        "ok": not errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка текста отклика")
    parser.add_argument("path", nargs="?", help="UTF-8 файл. Без пути текст читается из stdin.")
    parser.add_argument("--channel", type=str.casefold, choices=VALID_CHANNELS, default="headhunter")
    parser.add_argument(
        "--employer-question",
        action="store_true",
        help="Проверять как ответ на вопрос работодателя, без лимита короткого отклика.",
    )
    parser.add_argument(
        "--maximum-chars",
        type=int,
        help="Фактический максимум поля формы; имеет приоритет над профилем канала.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--length-unit", choices=["characters", "utf16"], default="characters",
                        help="Единица реального счётчика поля; UTF-16 для DOM maxlength.")
    parser.add_argument("--strict-warnings", action="store_true")
    args = parser.parse_args()

    if args.maximum_chars is not None and args.maximum_chars < 1:
        parser.error("--maximum-chars должен быть положительным числом")

    result = validate(
        read_text(args.path),
        args.channel,
        employer_question=args.employer_question,
        maximum_chars=args.maximum_chars,
        length_unit=args.length_unit,
    )
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        state = "OK" if result["ok"] else "ERROR"
        print(f"{state}: {result['characters_excluding_links']} знаков без ссылок")
        for item in result["errors"]:
            print(f"ERROR [{item['code']}]: {item['message']}")
        for item in result["warnings"]:
            print(f"WARN [{item['code']}]: {item['message']}")
        for item in result["notes"]:
            print(f"INFO [{item['code']}]: {item['message']}")

    if result["errors"]:
        return 1
    if args.strict_warnings and result["warnings"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
