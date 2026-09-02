#!/usr/bin/env python3
"""Editorial heuristics, not proven conversion optima. No minimum length requirement."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


CHANNELS = {
    "headhunter": {"target": (250, 550), "maximum": 650, "sentence_words": 28, "paragraph_chars": 260},
    "linkedin_dm": {"target": (180, 400), "maximum": 500, "sentence_words": 26, "paragraph_chars": 240},
    "linkedin_cover": {"target": (700, 1400), "maximum": 1600, "sentence_words": 32, "paragraph_chars": 420},
    "telegram": {"target": (180, 550), "maximum": 650, "sentence_words": 26, "paragraph_chars": 240},
    "email": {"target": (220, 550), "maximum": 700, "sentence_words": 28, "paragraph_chars": 260},
    "form_answer": {"target": None, "maximum": None, "sentence_words": 32, "paragraph_chars": 420},
}

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


def validate(text: str, channel: str) -> dict[str, object]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    notes: list[dict[str, str]] = []
    normalized = text.strip()
    length = visible_length(text)
    rules = CHANNELS[channel]

    if not normalized:
        errors.append(issue("empty", "Текст пуст."))
    if "\u2014" in text:
        errors.append(issue("em_dash", "Найдено длинное тире. Используй среднее тире или дефис."))
    if rules["maximum"] is not None and length > int(rules["maximum"]):
        errors.append(issue("too_long", f"{length} знаков без ссылок, максимум для канала – {rules['maximum']}."))

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

    if channel == "headhunter" and "с уважением" in lower:
        warnings.append(issue("hh_signature", "На HH формальная подпись обычно не нужна."))

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    if channel in {"headhunter", "linkedin_dm", "telegram", "email"} and len(paragraphs) > 4:
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
        "errors": errors,
        "warnings": warnings,
        "notes": notes,
        "ok": not errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка текста отклика")
    parser.add_argument("path", nargs="?", help="UTF-8 файл. Без пути текст читается из stdin.")
    parser.add_argument("--channel", choices=CHANNELS, default="headhunter")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--strict-warnings", action="store_true")
    args = parser.parse_args()

    result = validate(read_text(args.path), args.channel)
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
