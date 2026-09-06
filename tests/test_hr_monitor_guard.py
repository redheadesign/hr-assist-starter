import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hr_monitor_guard as guard  # noqa: E402


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2035, 9, 6, 12, tzinfo=guard.TIMEZONE)
        self.cfg = dict(guard.DEFAULT_POLICY)
        self.state = guard.empty_state()

    def test_begin_prearms_and_is_idempotent(self):
        first = guard.begin(self.state, "scan-1", self.now, self.cfg)
        again = guard.begin(self.state, "scan-1", self.now, self.cfg)
        self.assertTrue(first["authorized"])
        self.assertFalse(again["authorized"])
        self.assertEqual(self.state["attempts"]["scan-1"]["retry_not_before"], guard.iso(self.now + timedelta(minutes=240)))

    def test_error_backoff_caps_and_success_resets(self):
        for number, minutes in enumerate((240, 480, 1440, 1440), 1):
            key = f"scan-{number}"
            guard.begin(self.state, key, self.now, self.cfg)
            guard.finish(self.state, key, "error", "auth", ".local/evidence.json", self.now, self.cfg)
            self.assertEqual(guard.parse_time(self.state["cooldown_until"]), self.now + timedelta(minutes=minutes))
            self.now += timedelta(minutes=minutes)
        guard.begin(self.state, "success", self.now, self.cfg)
        guard.finish(self.state, "success", "success", None, ".local/evidence.json", self.now, self.cfg)
        self.assertEqual(self.state["consecutive_failures"], 0)
        self.assertIsNone(self.state["cooldown_until"])

    def test_crash_is_failure_after_prearmed_deadline(self):
        guard.begin(self.state, "crashed", self.now, self.cfg)
        later = self.now + timedelta(minutes=240)
        result = guard.begin(self.state, "next", later, self.cfg)
        self.assertFalse(result["authorized"])
        self.assertEqual(self.state["attempts"]["crashed"]["error_code"], "unfinished")
        self.assertEqual(self.state["consecutive_failures"], 1)

    def test_finish_and_diagnose_are_idempotent_and_bounded(self):
        guard.begin(self.state, "scan", self.now, self.cfg)
        recorded = guard.finish(self.state, "scan", "error", "network", ".local/evidence.json", self.now, self.cfg)
        duplicate = guard.finish(self.state, "scan", "error", "network", ".local/evidence.json", self.now, self.cfg)
        self.assertEqual(recorded["status"], "recorded")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertTrue(guard.diagnose(self.state, "scan", self.cfg)["authorized"])
        self.assertFalse(guard.diagnose(self.state, "scan", self.cfg)["authorized"])

    def test_status_is_read_only_and_checkpoints_survive(self):
        self.state["checkpoints"] = {"inbox": "kept"}
        before = repr(self.state)
        snapshot = guard._status(self.state, self.now, self.cfg)
        self.assertTrue(snapshot["may_scan"])
        self.assertEqual(repr(self.state), before)

    def test_expired_prearm_is_visible_but_success_has_no_floor(self):
        guard.begin(self.state, "stuck", self.now, self.cfg)
        snapshot = guard._status(self.state, self.now + timedelta(minutes=240), self.cfg)
        self.assertTrue(snapshot["may_scan"])
        self.assertTrue(snapshot["recovery_required"])
        guard._recover_crash(self.state, self.now + timedelta(minutes=240), self.cfg)
        self.assertEqual(self.state["consecutive_failures"], 1)
        guard.begin(self.state, "ok", self.now + timedelta(minutes=480), self.cfg)
        guard.finish(self.state, "ok", "success", None, ".local/evidence.json", self.now + timedelta(minutes=480), self.cfg)
        self.assertEqual(guard._status(self.state, self.now + timedelta(minutes=480), self.cfg)["interval_minutes_floor"], 0)

    def test_rejects_empty_and_outside_evidence(self):
        with self.assertRaises(guard.GuardError): guard.begin(self.state, " ", self.now, self.cfg)
        with self.assertRaises(guard.GuardError): guard.evidence(" ")
        with self.assertRaises(guard.GuardError): guard.evidence("C:/outside/evidence.json")

    def test_due_probe_cannot_override_error_cooldown(self):
        guard.begin(self.state, "scan", self.now, self.cfg)
        guard.finish(self.state, "scan", "error", "cua_timeout", ".local/reports/evidence.json", self.now, self.cfg)
        gate = guard._status(self.state, self.now, self.cfg)
        plan = {"mode": "post_campaign_probe", "interval_minutes": 0, "run_now": True}
        result = guard.apply_schedule(plan, gate, self.now)
        self.assertFalse(result["run_now"])
        self.assertEqual(result["interval_minutes"], 240)
        self.assertEqual(result["mode"], "error_backoff")
        self.assertTrue(plan["run_now"])

    def test_restart_persists_prearmed_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            guard.mutate(path, self.now, lambda state: guard.begin(state, "scan", self.now, self.cfg))
            restored = guard.load_state(path)
            self.assertEqual(restored["active_attempt_key"], "scan")


if __name__ == "__main__": unittest.main()
