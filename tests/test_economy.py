"""Synthetic checks for portable routing, cadence and privacy-safe economy controls."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class EconomyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "portable workspace"
        self.root.mkdir()
        for directory in ("scripts", "templates"):
            shutil.copytree(ROOT / directory, self.root / directory,
                            ignore=shutil.ignore_patterns("__pycache__"))
        self.run_script("setup", "init")

    def run_script(self, script, *args, success=True):
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(self.root / "scripts" / (script + ".py")), *args],
                                cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def run_code(self, code):
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code], cwd=self.root,
                                capture_output=True, text=True, encoding="utf-8",
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result.stdout

    def read(self, relative):
        return json.loads((self.root / ".local" / relative).read_text(encoding="utf-8"))

    def write(self, relative, value):
        path = self.root / ".local" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def routing(self):
        return self.read("config/model-routing.json")

    def test_routing_rejects_malformed_astra_and_unsupported_effort(self):
        config = self.routing()
        del config["routes"]["scout"]
        self.write("config/model-routing.json", config)
        self.run_script("hr_routing", "validate", success=False)
        config = json.loads((self.root / "templates/model-routing.json").read_text(encoding="utf-8"))
        config["routes"]["scout"]["fallbacks"][0]["model"] = "gpt-6-astra"
        self.write("config/model-routing.json", config)
        self.run_script("hr_routing", "validate", success=False)
        config = json.loads((self.root / "templates/model-routing.json").read_text(encoding="utf-8"))
        config["routes"]["scout"]["reasoning_effort"] = "ultra"
        self.write("config/model-routing.json", config)
        self.run_script("hr_routing", "validate", success=False)

    def test_routing_uses_configured_fallback_or_stops(self):
        routed = json.loads(self.run_script("hr_routing", "route", "--role", "scout", "--available-model", "gpt-5.6-terra").stdout)
        self.assertEqual((routed["model"], routed["reasoning_effort"], routed["route_index"]), ("gpt-5.6-terra", "medium", 1))
        self.run_script("hr_routing", "route", "--role", "scout", "--available-model", "gpt-6-astra", success=False)

    def test_setup_merges_routing_without_replacing_grants(self):
        permissions = self.read("config/permissions.json")
        permissions["standing_application_authorization"].update(active=True, id="synthetic-grant", source="synthetic")
        self.write("config/permissions.json", permissions)
        routing = self.routing()
        routing["telemetry_note"] = "keep-owner-config"
        self.write("config/model-routing.json", routing)
        self.run_script("setup", "init")
        self.assertEqual(self.read("config/permissions.json")["standing_application_authorization"]["id"], "synthetic-grant")
        self.assertEqual(self.read("config/model-routing.json")["telemetry_note"], "keep-owner-config")

    def test_compact_monitor_hides_statistics_and_evidence(self):
        settings = self.read("config/settings.json")
        settings["timezone"] = "UTC"
        settings["campaign"].update(enabled=True, sources=[{"id": "hh"}])
        self.write("config/settings.json", settings)
        today = datetime.now(timezone.utc).date().isoformat()
        state = {"version": 1, "days": {today: {"market_coverage": {"headhunter": {"state": "complete", "evidence": "private-proof"}}, "source_stats": {"headhunter": {"tokens": 99}}, "artifact_ref": ".local/reports/private"}}, "companies": {}, "blockers": {}}
        self.write("data/workflow-state.json", state)
        output = self.run_script("hr_context", "monitor").stdout
        self.assertNotIn("source_stats", output)
        self.assertNotIn("private-proof", output)
        full = self.run_script("hr_context", "monitor", "--full").stdout
        self.assertIn("source_stats", full)
        self.assertIn("private-proof", full)

    def test_bot_context_does_not_read_crm_or_mutate_state(self):
        crm = self.root / ".local/data/applications.jsonl"
        crm.unlink()
        crm.mkdir()
        checkpoint = self.write("data/bot-monitor-state.json", {"checkpoint": "synthetic"})
        before = checkpoint.read_bytes()
        output = self.run_script("hr_context", "bot-monitor").stdout
        self.assertIn("next_guide", output)
        self.assertEqual(checkpoint.read_bytes(), before)

    def test_cadence_groups_company_scans_and_respects_manual(self):
        settings = self.read("config/settings.json")
        settings["campaign"].update(sources=[{"id": "hh"}, {"id": "getmatch"}, {"id": "hirehi"}, {"id": "company_careers"}], source_cadence={
            "hh": {"frequency": "daily"}, "getmatch": {"frequency": "weekly", "weekdays": [0]},
            "hirehi": {"frequency": "manual"}, "company_careers": {"frequency": "weekly", "weekdays": [0]}})
        self.write("config/settings.json", settings)
        self.write("config/company-career-sources.json", {"companies": [{"id": "one", "scan_group": "group-a"}, {"id": "two", "scan_group": "group-a"}, {"id": "three"}]})
        lookup = "import json, sys; sys.path.insert(0, 'scripts'); import hr_workflow; print(json.dumps(hr_workflow.expected_sources(day=DAY)))"
        monday = json.loads(self.run_code(lookup.replace("DAY", repr("2030-01-07"))))
        tuesday = json.loads(self.run_code(lookup.replace("DAY", repr("2030-01-08"))))
        self.assertEqual(monday, ["company:group-a", "company:three", "getmatch", "headhunter"])
        self.assertEqual(tuesday, ["headhunter"])
        settings["campaign"]["sources"] = [{"id": "hh"}]
        self.write("config/settings.json", settings)
        self.assertEqual(json.loads(self.run_code(lookup.replace("DAY", repr("2030-01-07")))), ["headhunter"])

    def test_stopped_by_user_persists_in_day_state(self):
        settings = self.read("config/settings.json")
        settings["campaign"]["enabled"] = True
        self.write("config/settings.json", settings)
        payload = {"day": "2030-01-01", "campaign_state": "stopped_by_user", "artifact_ref": ".local/reports/stop.md"}
        payload_path = self.write("inputs/stop.json", payload)
        self.run_script("hr_workflow", "record-day", "--json", str(payload_path))
        first = json.loads(self.run_script("hr_workflow", "day", "--day", "2030-01-01").stdout)
        second = json.loads(self.run_script("hr_workflow", "day", "--day", "2030-01-01").stdout)
        self.assertEqual(first["campaign_state"], "stopped_by_user")
        self.assertEqual(second["campaign_state"], "stopped_by_user")

    def test_usage_is_explicit_through_aware_and_never_exports_event_content(self):
        rollout = self.root / "selected-rollout.jsonl"
        events = [
            {"timestamp": "2030-01-01T00:00:00Z", "type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "high"}},
            {"timestamp": "2030-01-01T00:00:01Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 2}}}},
            {"timestamp": "2030-01-01T00:00:02Z", "type": "response_item", "payload": {"type": "function_call", "arguments": "secret arguments"}},
            {"timestamp": "2030-01-01T00:00:03Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 150, "output_tokens": 5, "reasoning_output_tokens": 1}}}},
            {"timestamp": "2030-01-01T00:00:04Z", "type": "response_item", "payload": {"type": "message", "content": [{"text": "secret final text"}]}},
            [],
            {"timestamp": "2030-01-01T00:00:05", "type": "event_msg", "payload": []},
        ]
        rollout.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        output = self.run_script("hr_usage", str(rollout), "--through", "2030-01-01T00:00:03Z", "--output", "reports/usage.json").stdout
        report = json.loads(output)
        self.assertEqual(report["usage"], {"input_tokens": 150, "output_tokens": 5, "reasoning_output_tokens": 1})
        self.assertEqual(report["observed_models"], [{"model": "gpt-5.6-sol", "reasoning_effort": "high", "turns": 1}])
        self.assertEqual(report["top_level_tool_calls"], 1)
        self.assertEqual(report["duration_seconds"], 3.0)
        self.assertTrue(report["usage_complete"])
        self.assertEqual(report["missing_usage_count"], 0)
        self.assertEqual(json.loads((self.root / ".local/reports/usage.json").read_text(encoding="utf-8")), report)
        self.assertNotIn("secret", json.dumps(report))

    def test_usage_marks_missing_snapshots_and_rejects_duplicate_paths(self):
        known = self.root / "known.jsonl"
        unknown = self.root / "unknown.jsonl"
        negative = self.root / "negative.jsonl"
        known.write_text(json.dumps({"timestamp": "2030-01-01T00:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 2}}}}), encoding="utf-8")
        unknown.write_text(json.dumps({"timestamp": "2030-01-01T00:00:00Z", "type": "response_item", "payload": {"type": "function_call"}}), encoding="utf-8")
        negative.write_text(json.dumps({"timestamp": "2030-01-01T00:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": -1}}}}), encoding="utf-8")
        report = json.loads(self.run_script("hr_usage", str(known), str(unknown), str(negative)).stdout)
        self.assertEqual(report["usage"], {"input_tokens": 2})
        self.assertFalse(report["usage_complete"])
        self.assertEqual(report["missing_usage_count"], 2)
        only_unknown = json.loads(self.run_script("hr_usage", str(unknown)).stdout)
        self.assertIsNone(only_unknown["usage"])
        self.run_script("hr_usage", str(known), str(known), success=False)


if __name__ == "__main__":
    unittest.main()
