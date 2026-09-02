from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import hr_store  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.config_dir = self.root / "config"
        self.data_dir.mkdir()
        self.config_dir.mkdir()

        hr_store.ROOT = self.root
        hr_store.DATA_DIR = self.data_dir
        hr_store.ADAPTERS_PATH = self.config_dir / "adapters.json"
        hr_store.FILES = {
            "vacancy": self.data_dir / "vacancies.jsonl",
            "application": self.data_dir / "applications.jsonl",
            "event": self.data_dir / "events.jsonl",
            "feedback": self.data_dir / "feedback.jsonl",
            "experiment": self.data_dir / "experiments.jsonl",
            "outreach": self.data_dir / "outreach-targets.jsonl",
            "suppression": self.data_dir / "suppressions.jsonl",
            "adapter_scan": self.data_dir / "adapter-scans.jsonl",
        }
        hr_store.ADAPTERS_PATH.write_text(
            json.dumps(
                {
                    "version": 2,
                    "updated_at": "2035-01-02",
                    "adapters": {
                        "headhunter_browser": {
                            "status": "auth_required",
                            "transport": "browser",
                            "mode": "read_only",
                            "target_url": "https://hh.ru/applicant/negotiations",
                            "read_enabled": False,
                            "needs_user_action": True,
                            "control_runs_remaining": 3,
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        hr_store.ensure_files()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def payload(self, value: dict[str, object]) -> str:
        path = self.root / f"payload-{len(list(self.root.glob('payload-*.json')))}.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def call(self, function: object, payload: dict[str, object]) -> dict[str, object]:
        output = io.StringIO()
        args = argparse.Namespace(json_path=self.payload(payload))
        with contextlib.redirect_stdout(output):
            result = function(args)
        self.assertEqual(result, 0)
        return json.loads(output.getvalue())

    def test_unsupported_adapter_cannot_be_enabled(self) -> None:
        data = {"adapters": {"headhunter_api": {
            "status": "unsupported", "transport": "official_api", "read_enabled": True,
        }}}
        for enabled, expected in ((True, 1), (False, 0)):
            data["adapters"]["headhunter_api"]["read_enabled"] = enabled
            hr_store.ADAPTERS_PATH.write_text(json.dumps(data), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hr_store.command_validate(argparse.Namespace()), expected)

    def test_observed_application_is_idempotent(self) -> None:
        payload = {
            "source": "headhunter",
            "source_application_key": "hh-123",
            "company": "Пример",
            "title": "Продуктовый дизайнер",
            "vacancy_url": "https://example.invalid/vacancy/demo",
            "application_url": "https://example.invalid/application/demo",
            "sent_at": "2035-01-01T10:00:00+03:00",
            "imported": True,
        }
        first = self.call(hr_store.command_observe_application, payload)
        second = self.call(hr_store.command_observe_application, payload)

        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "exists")
        self.assertEqual(len(hr_store.load_jsonl(hr_store.FILES["vacancy"])), 1)
        self.assertEqual(len(hr_store.load_jsonl(hr_store.FILES["application"])), 1)
        self.assertEqual(len(hr_store.load_jsonl(hr_store.FILES["event"])), 1)

    def test_observed_application_stores_cover_and_platform_day(self) -> None:
        cover = "Короткий текст отклика"
        result = self.call(
            hr_store.command_observe_application,
            {
                "source": "headhunter",
                "source_application_key": "hh-cover-1",
                "company": "Пример",
                "title": "Продуктовый дизайнер",
                "sent_at": "2035-01-02T00:00:00+03:00",
                "date_quality": "platform_day",
                "cover_letter_text": cover,
                "cover_letter_issue_flags": ["example_flag"],
            },
        )
        application = hr_store.latest_by_id(hr_store.load_jsonl(hr_store.FILES["application"]))[result["id"]]
        self.assertEqual(application["date_quality"], "platform_day")
        self.assertEqual(application["cover_letter_text"], cover)
        self.assertEqual(application["cover_letter_char_count"], len(cover))
        self.assertEqual(application["cover_letter_issue_flags"], ["example_flag"])

    def test_source_event_key_prevents_duplicates(self) -> None:
        observed = self.call(
            hr_store.command_observe_application,
            {
                "source": "hirehi",
                "source_application_key": "hirehi-7",
                "company": "Пример",
                "title": "UX/UI дизайнер",
            },
        )
        event = {
            "application_id": observed["id"],
            "type": "reply",
            "source": "hirehi",
            "source_event_key": "hirehi:hirehi-7:message-9",
            "source_application_key": "hirehi-7",
            "message_text": "Здравствуйте",
            "confidence": "high",
        }
        first = self.call(hr_store.command_add_event, event)
        second = self.call(hr_store.command_add_event, event)

        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "exists")
        self.assertEqual(len(hr_store.load_jsonl(hr_store.FILES["event"])), 2)

    def test_auto_rejection_text_is_not_stored(self) -> None:
        observed = self.call(
            hr_store.command_observe_application,
            {
                "source": "getmatch",
                "source_application_key": "gm-3",
                "company": "Пример",
                "title": "Product Designer",
            },
        )
        self.call(
            hr_store.command_add_feedback,
            {
                "application_id": observed["id"],
                "raw_text": "Спасибо, но мы решили продолжить с другими кандидатами",
                "rejection_type": "auto_bot",
                "source": "getmatch",
            },
        )
        feedback = hr_store.load_jsonl(hr_store.FILES["feedback"])[0]
        self.assertNotIn("raw_text", feedback)
        self.assertTrue(feedback["raw_text_omitted"])
        self.assertEqual(len(feedback["raw_text_fingerprint"]), 64)
        self.call(hr_store.command_add_event, {
            "application_id": observed["id"], "type": "rejection",
            "rejection_type": "auto_bot", "message_text": "Synthetic template",
            "raw_text": "Second synthetic copy", "source_event_key": "demo:auto:1",
        })
        event = hr_store.load_jsonl(hr_store.FILES["event"])[-1]
        self.assertNotIn("raw_text", event)
        self.assertNotIn("message_text", event)
        self.assertTrue(event["template_text_omitted"])

    def test_missing_sent_date_is_excluded_from_cohort(self):
        self.call(hr_store.command_observe_application, {
            "source": "example", "source_application_key": "undated",
            "company": "Example", "title": "Example role",
        })
        application = hr_store.load_jsonl(hr_store.FILES["application"])[0]
        self.assertIsNone(application["sent_at"])
        self.assertFalse(application["include_in_period_metrics"])
        from datetime import datetime, timedelta
        now = datetime.now(hr_store.WORKSPACE_TZ)
        summary = hr_store.build_summary(now - timedelta(days=1), now + timedelta(days=1))
        self.assertIn("Отправлено: **0**", summary)

    def test_manual_probe_enables_adapter_without_consuming_scheduled_controls(self) -> None:
        self.call(
            hr_store.command_record_adapter_scan,
            {
                "adapter": "headhunter_browser",
                "status": "available",
                "scan_succeeded": True,
                "enable_read": True,
                "items_seen": 4,
                "events_added": 2,
            },
        )
        config = hr_store.load_json(hr_store.ADAPTERS_PATH)
        adapter = config["adapters"]["headhunter_browser"]
        self.assertTrue(adapter["read_enabled"])
        self.assertTrue(adapter["initial_probe_completed"])
        self.assertFalse(adapter["needs_user_action"])
        self.assertEqual(adapter["control_runs_remaining"], 3)
        self.call(hr_store.command_record_adapter_scan, {"adapter": "headhunter_browser", "status": "temporary_error", "scan_succeeded": False, "scheduled_run": True})
        self.assertEqual(hr_store.load_json(hr_store.ADAPTERS_PATH)["adapters"]["headhunter_browser"]["control_runs_remaining"], 3)
        self.call(hr_store.command_record_adapter_scan, {"adapter": "headhunter_browser", "status": "available", "scan_succeeded": True, "scheduled_run": True, "at": "2035-01-03T10:00:00+03:00"})
        self.assertEqual(hr_store.load_json(hr_store.ADAPTERS_PATH)["adapters"]["headhunter_browser"]["control_runs_remaining"], 2)
        self.call(hr_store.command_record_adapter_scan, {"adapter": "headhunter_browser", "status": "available", "scan_succeeded": True, "scheduled_run": True, "at": "2035-01-03T10:20:00+03:00"})
        self.assertEqual(hr_store.load_json(hr_store.ADAPTERS_PATH)["adapters"]["headhunter_browser"]["control_runs_remaining"], 2)
        self.call(hr_store.command_record_adapter_scan, {"adapter": "headhunter_browser", "status": "available", "scan_succeeded": True, "scheduled_run": True, "at": "2035-01-04T10:00:00+03:00"})
        self.assertEqual(hr_store.load_json(hr_store.ADAPTERS_PATH)["adapters"]["headhunter_browser"]["control_runs_remaining"], 1)
        self.assertIsNotNone(adapter["last_successful_scan_at"])

    def test_failed_state_cannot_advance_checkpoint(self):
        before=hr_store.load_json(hr_store.ADAPTERS_PATH)
        with self.assertRaises(hr_store.StoreError):
            self.call(hr_store.command_record_adapter_scan,{"adapter":"headhunter_browser","status":"captcha_required","scan_succeeded":True})
        self.assertEqual(before,hr_store.load_json(hr_store.ADAPTERS_PATH))


if __name__ == "__main__":
    unittest.main()
