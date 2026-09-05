from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import hr_campaign  # noqa: E402


WORKSPACE_TZ = hr_campaign.WORKSPACE_TZ


class CampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = hr_campaign.empty_state("coordinator-1")
        self.now = datetime(2026, 9, 5, 12, 0, tzinfo=WORKSPACE_TZ)

    def add(self, task_type: str, key: str | None = None) -> dict[str, object]:
        status, task = hr_campaign.add_task(
            self.state, task_type, "headhunter", self.now, canonical_key=key
        )
        self.assertEqual(status, "created")
        return task

    def test_canonical_key_is_idempotent_across_platforms(self) -> None:
        status, first = hr_campaign.add_task(
            self.state, "application", "headhunter", self.now,
            canonical_key=" ACME / Product Designer ",
        )
        duplicate_status, duplicate = hr_campaign.add_task(
            self.state, "application", "getmatch", self.now,
            canonical_key="acme / product designer",
        )
        next_day_application_status, _ = hr_campaign.add_task(
            self.state, "application", "hirehi", self.now + timedelta(days=1),
            canonical_key="acme / product designer",
        )

        self.assertEqual(status, "created")
        self.assertEqual(duplicate_status, "exists")
        self.assertEqual(next_day_application_status, "exists")
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(len(self.state["tasks"]), 1)

        scan_status, _ = hr_campaign.add_task(
            self.state, "source_scan", "headhunter", self.now,
            canonical_key="acme / product designer",
        )
        same_day_scan_status, _ = hr_campaign.add_task(
            self.state, "source_scan", "getmatch", self.now,
            canonical_key="acme / product designer",
        )
        next_day_status, _ = hr_campaign.add_task(
            self.state, "source_scan", "headhunter", self.now + timedelta(days=1),
            canonical_key="acme / product designer",
        )
        self.assertEqual(scan_status, "created")
        self.assertEqual(same_day_scan_status, "exists")
        self.assertEqual(next_day_status, "created")
        self.assertEqual(len(self.state["tasks"]), 3)

    def test_claim_is_exclusive_and_completed_task_is_retained_same_day(self) -> None:
        self.add("source_scan")
        claimed, uncertain = hr_campaign.claim_task(self.state, "worker-a", self.now)
        second, _ = hr_campaign.claim_task(self.state, "worker-b", self.now)

        self.assertEqual(uncertain, [])
        self.assertIsNotNone(claimed)
        self.assertIsNone(second)
        finished = hr_campaign.finish_task(
            self.state, str(claimed["id"]), self.now,
            "completed", str(claimed["claim_token"]), "artifacts/scan.json",
        )
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(len(self.state["tasks"]), 1)

    def test_claim_can_target_a_specific_due_chat(self) -> None:
        first = self.add("bot_chat", "chat-1")
        second = self.add("bot_chat", "chat-2")
        claimed, _ = hr_campaign.claim_task(
            self.state, "worker-chat", self.now, task_id=str(second["id"])
        )
        self.assertEqual(claimed["id"], second["id"])
        self.assertEqual(first["status"], "pending")

    def test_risky_tasks_require_nonempty_canonical_key(self) -> None:
        for task_type in ("application", "bot_chat"):
            for key in (None, "", "   "):
                with self.subTest(task_type=task_type, key=key):
                    before = len(self.state["tasks"])
                    with self.assertRaisesRegex(
                        hr_campaign.CampaignError, "canonical_key обязателен"
                    ):
                        hr_campaign.add_task(
                            self.state, task_type, "headhunter", self.now,
                            canonical_key=key,
                        )
                    self.assertEqual(len(self.state["tasks"]), before)

    def test_source_scan_without_canonical_key_remains_supported(self) -> None:
        status, task = hr_campaign.add_task(
            self.state, "source_scan", "headhunter", self.now
        )
        self.assertEqual(status, "created")
        self.assertIsNone(task["canonical_key"])

    def test_stale_application_becomes_uncertain_until_explicit_reconciliation(self) -> None:
        self.add("application", "stale-application")
        claimed, _ = hr_campaign.claim_task(
            self.state, "worker-a", self.now, lease_seconds=30
        )
        later = self.now + timedelta(seconds=31)
        replacement, uncertain = hr_campaign.claim_task(self.state, "worker-b", later)

        self.assertIsNone(replacement)
        self.assertEqual(uncertain, [claimed["id"]])
        self.assertEqual(claimed["status"], "uncertain")
        self.assertTrue(claimed["needs_reconciliation"])
        with self.assertRaises(hr_campaign.CampaignError):
            hr_campaign.finish_task(
                self.state, str(claimed["id"]), later, "retry", reconciled=False
            )
        recovered = hr_campaign.finish_task(
            self.state, str(claimed["id"]), later, "retry", reconciled=True
        )
        self.assertEqual(recovered["status"], "pending")
        self.assertFalse(recovered["needs_reconciliation"])

    def wait_again(
        self, task: dict[str, object], timestamp: datetime, **events: object
    ) -> dict[str, object]:
        claimed, _ = hr_campaign.claim_task(
            self.state, "worker-chat", timestamp, task_id=str(task["id"])
        )
        return hr_campaign.defer_chat(
            self.state, str(task["id"]), timestamp,
            str(claimed["claim_token"]), **events,
        )

    def test_repeated_polls_do_not_reset_idle_deadline(self) -> None:
        task = self.add("bot_chat", "chat-stable-deadline")
        original_deadline = task["idle_deadline_at"]
        waiting = self.wait_again(task, self.now)
        self.assertEqual(waiting["next_check_at"], "2026-09-05T12:05:00+00:00")

        for minute in (5, 10, 15):
            due = self.now + timedelta(minutes=minute)
            hr_campaign.activate_due(self.state, due)
            waiting = self.wait_again(task, due)
            self.assertEqual(waiting["idle_deadline_at"], original_deadline)
            self.assertFalse(waiting["last_poll_meaningful"])
            self.assertEqual(
                waiting["next_check_at"], hr_campaign.iso(due + timedelta(minutes=5))
            )

    def test_due_persists_idle_timeout_at_exactly_sixty_minutes(self) -> None:
        task = self.add("bot_chat", "chat-expire")
        self.wait_again(task, self.now)

        hr_campaign.activate_due(self.state, self.now + timedelta(minutes=59, seconds=59))
        self.assertNotEqual(task["status"], "dormant")
        hr_campaign.activate_due(self.state, self.now + timedelta(hours=1))

        self.assertEqual(task["status"], "dormant")
        self.assertEqual(task["outcome_reason"], "idle_timeout")
        self.assertNotEqual(task.get("outcome_reason"), "completed_screening")
        self.assertNotIn("next_check_at", task)

    def test_due_expires_one_chat_without_stopping_another(self) -> None:
        expired = self.add("bot_chat", "chat-expired")
        active = self.add("bot_chat", "chat-active")
        self.wait_again(expired, self.now)
        self.wait_again(active, self.now)

        at_half_hour = self.now + timedelta(minutes=30)
        hr_campaign.activate_due(self.state, at_half_hour)
        self.wait_again(
            active, at_half_hour, incoming=True, incoming_message_id="bot-msg-2"
        )
        activated = hr_campaign.activate_due(self.state, self.now + timedelta(hours=1))

        self.assertEqual(expired["status"], "dormant")
        self.assertEqual([task["id"] for task in activated], [active["id"]])
        self.assertEqual(active["status"], "pending")
        self.assertEqual(active["idle_deadline_at"], "2026-09-05T13:30:00+00:00")

    def test_late_incoming_reopens_once_by_message_identity(self) -> None:
        task = self.add("bot_chat", "chat-reopen")
        self.wait_again(task, self.now)
        hr_campaign.activate_due(self.state, self.now + timedelta(hours=1))

        with self.assertRaises(hr_campaign.CampaignError):
            hr_campaign.resume_chat(
                self.state, str(task["id"]), self.now + timedelta(hours=2), True
            )
        reopened = hr_campaign.resume_chat(
            self.state, str(task["id"]), self.now + timedelta(hours=2), True,
            message_id="late-message-7",
        )
        duplicate = hr_campaign.resume_chat(
            self.state, str(task["id"]), self.now + timedelta(hours=2, minutes=1), True,
            message_id="late-message-7",
        )

        self.assertEqual(reopened["status"], "pending")
        self.assertIs(duplicate, task)
        self.assertTrue(duplicate["resume_duplicate"])
        self.assertEqual(duplicate["incoming_count"], 1)
        self.assertEqual(duplicate["idle_deadline_at"], "2026-09-05T15:00:00+00:00")

    def test_closing_response_waits_and_cannot_fake_screening_success(self) -> None:
        task = self.add("bot_chat", "chat-closing")
        waiting = self.wait_again(
            task,
            self.now,
            incoming=True,
            incoming_message_id="bot-any-questions",
            response_sent=True,
            response_message_id="candidate-no-more-questions",
            closing=True,
        )
        self.assertEqual(waiting["status"], "waiting")
        self.assertTrue(waiting["closing"])
        self.assertNotEqual(waiting.get("status"), "completed")

        hr_campaign.activate_due(self.state, self.now + timedelta(minutes=5))
        claimed, _ = hr_campaign.claim_task(
            self.state, "worker-chat", self.now + timedelta(minutes=5),
            task_id=str(task["id"]),
        )
        with self.assertRaises(hr_campaign.CampaignError):
            hr_campaign.finish_task(
                self.state, str(task["id"]), self.now + timedelta(minutes=5),
                "completed", str(claimed["claim_token"]),
            )
        self.assertEqual(task["status"], "running")

        completed = hr_campaign.finish_task(
            self.state, str(task["id"]), self.now + timedelta(minutes=5),
            "completed", str(claimed["claim_token"]), explicit_completion=True,
        )
        self.assertEqual(completed["outcome_reason"], "explicit_bot_completion")

    def test_midnight_does_not_defer_or_reset_deadline(self) -> None:
        late = datetime(2026, 9, 5, 23, 58, tzinfo=WORKSPACE_TZ)
        _, task = hr_campaign.add_task(
            self.state, "bot_chat", "getmatch", late, canonical_key="midnight"
        )
        claimed, _ = hr_campaign.claim_task(
            self.state, "worker-chat", late, task_id=str(task["id"])
        )
        waiting = hr_campaign.defer_chat(
            self.state, str(task["id"]), late, str(claimed["claim_token"])
        )

        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(waiting["next_check_at"], "2026-09-06T00:03:00+00:00")
        self.assertEqual(waiting["idle_deadline_at"], "2026-09-06T00:58:00+00:00")
        activated = hr_campaign.activate_due(
            self.state, datetime(2026, 9, 6, 0, 3, tzinfo=WORKSPACE_TZ)
        )
        self.assertEqual([item["id"] for item in activated], [task["id"]])
        self.assertEqual(task["day"], "2026-09-06")

    def test_schedule_mode_is_read_only_and_excludes_attention_states(self) -> None:
        uncertain = self.add("bot_chat", "chat-uncertain")
        uncertain["status"] = "uncertain"
        needs_user = self.add("bot_chat", "chat-needs-user")
        needs_user["status"] = "needs_user"
        snapshot = repr(self.state)

        idle = hr_campaign.schedule_mode(self.state, self.now)
        self.assertEqual(idle["mode"], "idle")
        self.assertEqual(idle["interval_minutes"], 240)
        self.assertEqual(repr(self.state), snapshot)

        active = self.add("bot_chat", "chat-hot")
        plan = hr_campaign.schedule_mode(self.state, self.now)
        self.assertEqual(plan["mode"], "active")
        self.assertEqual(plan["interval_minutes"], 5)
        self.assertEqual(plan["active_chat_ids"], [active["id"]])
        self.assertEqual(plan["timing"]["short_wait_seconds"], [10, 30, 60])

        expired_plan = hr_campaign.schedule_mode(
            self.state, self.now + timedelta(hours=1)
        )
        self.assertEqual(expired_plan["mode"], "idle")
        self.assertEqual(expired_plan["interval_minutes"], 240)

    def test_post_campaign_is_idempotent_and_retry_does_not_rebase(self) -> None:
        status, batch = hr_campaign.schedule_post_campaign(
            self.state, " 2026-09-05 / Daily HH ", self.now, self.now
        )
        retry_status, retry = hr_campaign.schedule_post_campaign(
            self.state,
            "2026-09-05 / daily hh",
            self.now + timedelta(minutes=30),
            self.now + timedelta(minutes=30),
        )

        self.assertEqual(status, "created")
        self.assertEqual(retry_status, "exists")
        self.assertIs(retry, batch)
        self.assertEqual(batch["completed_at"], "2026-09-05T12:00:00+00:00")
        self.assertEqual(
            [probe["due_at"] for probe in batch["probes"]],
            [
                "2026-09-05T12:05:00+00:00",
                "2026-09-05T12:10:00+00:00",
                "2026-09-05T13:00:00+00:00",
            ],
        )

    def test_late_probe_scan_satisfies_all_missed_deadlines_once(self) -> None:
        hr_campaign.schedule_post_campaign(
            self.state, "campaign-late", self.now, self.now
        )
        late = self.now + timedelta(minutes=61)
        due = hr_campaign.due_post_campaign_probes(self.state, late)
        before_read = repr(self.state)

        self.assertEqual(due["due_probe_count"], 3)
        self.assertEqual(due["campaigns"][0]["due_probe_minutes"], [5, 10, 60])
        self.assertEqual(due["action"], "read_inboxes_only")
        self.assertEqual(repr(self.state), before_read)

        status, attempt = hr_campaign.acknowledge_post_campaign_probes(
            self.state, late, "success", "inbox-scan-20260905-1301"
        )
        duplicate_status, duplicate = hr_campaign.acknowledge_post_campaign_probes(
            self.state,
            late + timedelta(minutes=1),
            "success",
            "inbox-scan-20260905-1301",
        )
        self.assertEqual(status, "recorded")
        self.assertEqual(attempt["satisfied_probe_count"], 3)
        self.assertEqual(duplicate_status, "exists")
        self.assertIs(duplicate, attempt)
        self.assertEqual(len(self.state["probe_attempts"]), 1)
        self.assertFalse(
            hr_campaign.due_post_campaign_probes(self.state, late)["scan_required"]
        )

    def test_probe_error_is_terminal_until_next_defined_probe(self) -> None:
        hr_campaign.schedule_post_campaign(
            self.state, "campaign-auth", self.now, self.now
        )
        first_due = self.now + timedelta(minutes=5)
        status, attempt = hr_campaign.acknowledge_post_campaign_probes(
            self.state,
            first_due,
            "error",
            "scan-auth-error-1",
            error_code="auth_required",
        )
        self.assertEqual(status, "recorded")
        self.assertEqual(attempt["outcome"], "error")
        self.assertEqual(attempt["satisfied_probe_count"], 1)
        self.assertEqual(
            hr_campaign.due_post_campaign_probes(
                self.state, first_due + timedelta(minutes=1)
            )["status"],
            "empty",
        )

        plan = hr_campaign.schedule_mode(
            self.state, first_due + timedelta(minutes=1)
        )
        self.assertEqual(plan["mode"], "post_campaign_probe")
        self.assertEqual(plan["interval_minutes"], 4)
        self.assertEqual(plan["next_run_at"], "2026-09-05T12:10:00+00:00")

    def test_schedule_jumps_from_ten_to_sixty_without_empty_hot_polls(self) -> None:
        hr_campaign.schedule_post_campaign(
            self.state, "campaign-gap", self.now, self.now
        )
        at_ten = self.now + timedelta(minutes=10)
        _, attempt = hr_campaign.acknowledge_post_campaign_probes(
            self.state, at_ten, "success", "scan-at-ten"
        )
        self.assertEqual(attempt["satisfied_probe_count"], 2)

        plan = hr_campaign.schedule_mode(self.state, at_ten)
        self.assertEqual(plan["mode"], "post_campaign_probe")
        self.assertEqual(plan["interval_minutes"], 50)
        self.assertEqual(plan["next_run_at"], "2026-09-05T13:00:00+00:00")

    def test_post_campaign_probes_cross_midnight_without_rebase(self) -> None:
        completed = datetime(2026, 9, 5, 23, 58, tzinfo=WORKSPACE_TZ)
        _, batch = hr_campaign.schedule_post_campaign(
            self.state, "campaign-midnight", completed, completed
        )
        self.assertEqual(
            [probe["due_at"] for probe in batch["probes"]],
            [
                "2026-09-06T00:03:00+00:00",
                "2026-09-06T00:08:00+00:00",
                "2026-09-06T00:58:00+00:00",
            ],
        )

    def test_renew_requires_same_worker_and_unexpired_lease(self) -> None:
        task = self.add("application", "renew-app")
        claimed, _ = hr_campaign.claim_task(
            self.state, "worker-a", self.now, task_id=str(task["id"]),
            lease_seconds=60,
        )
        renewed = hr_campaign.renew_lease(
            self.state, str(task["id"]), self.now + timedelta(seconds=30),
            str(claimed["claim_token"]), "worker-a", lease_seconds=90,
        )
        self.assertEqual(renewed["lease_expires_at"], "2026-09-05T12:02:00+00:00")
        with self.assertRaises(hr_campaign.CampaignError):
            hr_campaign.renew_lease(
                self.state, str(task["id"]), self.now + timedelta(minutes=2),
                str(claimed["claim_token"]), "worker-a",
            )
        hr_campaign.recover_stale(self.state, self.now + timedelta(minutes=2))
        self.assertEqual(task["status"], "uncertain")

    def test_cli_exposes_adaptive_chat_contract(self) -> None:
        parser = hr_campaign.build_parser()
        defer = parser.parse_args([
            "defer-chat", "--task-id", "task-1", "--claim-token", "claim-1",
            "--incoming", "--incoming-message-id", "msg-1",
            "--response-sent", "--response-fingerprint", "reply-fp", "--closing",
        ])
        finish = parser.parse_args([
            "finish", "--task-id", "task-1", "--outcome", "completed",
            "--explicit-completion",
        ])
        renew = parser.parse_args([
            "renew-lease", "--task-id", "task-1", "--worker-id", "worker-1",
            "--claim-token", "claim-1",
        ])
        schedule = parser.parse_args(["schedule-mode"])
        post_campaign = parser.parse_args([
            "post-campaign", "--campaign-key", "daily-2026-09-05"
        ])
        due_probes = parser.parse_args(["due-probes"])
        ack_probe = parser.parse_args([
            "ack-probe", "--attempt-key", "scan-1", "--outcome", "error",
            "--error-code", "auth_required",
        ])

        self.assertEqual(defer.coordinator_id, "hr-main")
        self.assertTrue(defer.closing)
        self.assertTrue(finish.explicit_completion)
        self.assertEqual(renew.command, "renew-lease")
        self.assertEqual(schedule.command, "schedule-mode")
        self.assertEqual(post_campaign.command, "post-campaign")
        self.assertEqual(due_probes.command, "due-probes")
        self.assertEqual(ack_probe.command, "ack-probe")


if __name__ == "__main__":
    unittest.main()
