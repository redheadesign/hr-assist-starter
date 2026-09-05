"""Synthetic end-to-end tests for the portable automation contract."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'portable workspace'
        self.root.mkdir()
        for directory in ('scripts', 'templates'):
            shutil.copytree(ROOT / directory, self.root / directory,
                            ignore=shutil.ignore_patterns('__pycache__'))
        self.run_script('setup', 'init')

    def run_script(self, script, *args, success=True):
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8',
                                 str(self.root / 'scripts' / (script + '.py')), *args],
                                cwd=self.root, capture_output=True, text=True, encoding='utf-8',
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result.stdout

    def get(self, relative):
        return json.loads((self.root / '.local' / relative).read_text(encoding='utf-8'))

    def put(self, relative, value):
        path = self.root / '.local' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return str(path)

    def configure(self):
        settings = self.get('config/settings.json')
        settings['target'].update(roles=['Synthetic role'], formats=['remote'],
                                  employment=['full_time'], languages={}, test_before_interview=False)
        settings['campaign'].update(enabled=True, daily_minimum=2, daily_maximum=3,
                                    sources=[{'id': 'hh'}])
        self.put('config/settings.json', settings)
        permissions = self.get('config/permissions.json')
        permissions.update(mode='limited_send', send_applications=True)
        permissions['standing_application_authorization'].update(
            active=True, id='synthetic-grant', granted_on='2020-01-01',
            source='Synthetic explicit test instruction', platforms=['headhunter'],
            data=['Synthetic profile'], resume_refs={'headhunter': 'https://example.invalid/cv'})
        self.put('config/permissions.json', permissions)
        self.put('knowledge/facts.json', [{'id': 'fact-1', 'claim': 'Synthetic confirmed work',
                                         'source': 'Synthetic fixture', 'confirmed': True, 'visibility': 'public'}])

    def packet(self):
        return {'platform': 'hh', 'canonical_key': 'synthetic-company/requisition-1',
                'vacancy_url': 'https://example.invalid/jobs/1', 'resume_ref': 'https://example.invalid/cv',
                'matched_fact_ids': ['fact-1'], 'cover_letter_text': 'Работал над похожими задачами. Могу рассказать о своём опыте',
                'campaign_id': 'synthetic-batch',
                'vacancy': {'open': True, 'role_match': True, 'company_risk_checked': True,
                            'company_safe': True, 'work_format': 'remote', 'employment': 'full_time',
                            'language_requirements': {}, 'test_before_interview': False, 'score': 90}}

    def build(self, packet):
        path = self.put('inputs/packet.json', packet)
        return json.loads(self.run_script('hr_workflow', 'packet', '--json', path))

    def review(self, packet):
        result = self.build(packet)
        packet['review'] = {'fingerprint': result['review_fingerprint'], 'outcome': 'pass',
                            'reviewer': 'synthetic-reviewer', 'artifact_ref': '.local/reports/review.md'}
        return self.build(packet)

    def test_fresh_setup_has_empty_state_and_no_outgoing_permissions(self):
        for operation in ('application', 'bot_reply', 'monitor', 'human_reply', 'profile_edit'):
            result = json.loads(self.run_script('hr_policy', 'authorize', '--operation', operation, '--platform', 'hh'))
            self.assertFalse(result['allowed'])
        state = json.loads(self.run_script('hr_workflow', 'day'))
        self.assertEqual(state['today_sent_count'], 0)
        self.assertEqual(state['campaign_state'], 'disabled')
        context = json.loads(self.run_script('hr_context', 'monitor', '--platform', 'hh'))
        self.assertIn('day_state', context)
        self.run_script('hr_campaign', 'schedule-mode')
        self.run_script('hr_campaign', 'list')
        self.run_script('hr_store', 'dashboard')
        self.assertTrue((self.root / '.local/DASHBOARD.md').exists())
        self.assertFalse((self.root / 'DASHBOARD.md').exists())

    def test_upgrade_preserves_existing_profile_history_and_grants(self):
        self.configure()
        settings = self.get('config/settings.json')
        del settings['campaign']['catch_up_enabled']
        self.put('config/settings.json', settings)
        self.put('config/profile.json', {'name': 'Synthetic candidate', 'experience': []})
        event = self.root / '.local/data/events.jsonl'
        event.write_text('{"id":"synthetic-event"}\n', encoding='utf-8')
        grant = self.get('config/permissions.json')
        self.run_script('setup', 'init')
        self.assertEqual(self.get('config/permissions.json'), grant)
        self.assertEqual(self.get('config/profile.json')['name'], 'Synthetic candidate')
        self.assertIn('catch_up_enabled', self.get('config/settings.json')['campaign'])
        self.assertIn('synthetic-event', event.read_text(encoding='utf-8'))

    def test_packet_requires_review_and_invalidates_it_after_change(self):
        self.configure()
        packet = self.packet()
        self.assertFalse(self.build(packet)['ready'])
        self.assertTrue(self.review(packet)['ready'])
        packet['cover_letter_text'] += '. Ещё один факт'
        self.assertFalse(self.build(packet)['ready'])
        self.assertTrue(self.review(packet)['ready'])
        facts = self.get('knowledge/facts.json')
        facts[0]['confirmed'] = False
        self.put('knowledge/facts.json', facts)
        result = self.build(packet)
        self.assertFalse(result['ready'])
        self.assertIn('confirmed_public_fact_ids', result['missing'])

    def test_revoked_expired_and_uncovered_grants_block_sends(self):
        self.configure()
        args = ('authorize', '--operation', 'application', '--platform', 'hh')
        self.assertTrue(json.loads(self.run_script('hr_policy', *args))['allowed'])
        permissions = self.get('config/permissions.json')
        for change in ({'active': False}, {'expires_at': '2020-01-01T00:00:00Z'},
                       {'platforms': ['getmatch']}, {'source': None}):
            candidate = json.loads(json.dumps(permissions))
            candidate['standing_application_authorization'].update(change)
            self.put('config/permissions.json', candidate)
            self.assertFalse(json.loads(self.run_script('hr_policy', *args))['allowed'])

    def test_bot_grant_is_separate_and_explicit(self):
        self.configure()
        args = ('authorize', '--operation', 'bot_reply', '--platform', 'hh')
        self.assertFalse(json.loads(self.run_script('hr_policy', *args))['allowed'])
        permissions = self.get('config/permissions.json')
        permissions['reply_to_vacancy_bots'] = True
        permissions['bot_dialog_authorization'].update(active=True, id='synthetic-bot-grant',
            granted_on='2020-01-01', source='Synthetic instruction', allowed_topics=['experience'])
        self.put('config/permissions.json', permissions)
        self.assertTrue(json.loads(self.run_script('hr_policy', *args))['allowed'])

    def test_claim_state_cannot_escape_private_directory(self):
        self.run_script('hr_campaign', '--state', str(self.root / 'exposed.json'), 'add',
                        '--type', 'application', '--platform', 'hh', '--canonical-key', 'synthetic', success=False)
        self.assertFalse((self.root / 'exposed.json').exists())

    def test_day_uses_configured_timezone_and_report_does_not_close_it(self):
        self.configure()
        settings = self.get('config/settings.json')
        settings['timezone'] = 'Asia/Tokyo'
        self.put('config/settings.json', settings)
        app = {'id': 'synthetic-app', 'sent_at': '2035-01-01T17:00:00+00:00',
               'record_origin': 'platform_observed', 'canonical_key': 'synthetic'}
        path = self.root / '.local/data/applications.jsonl'
        path.write_text(json.dumps(app) + '\n', encoding='utf-8')
        report = self.root / '.local/reports/daily/2035-01-02.md'
        report.write_text('Synthetic report', encoding='utf-8')
        result = json.loads(self.run_script('hr_workflow', 'day', '--day', '2035-01-02'))
        self.assertEqual(result['today_sent_count'], 1)
        self.assertTrue(result['report_exists'])
        self.assertEqual(result['campaign_state'], 'in_progress')
        self.assertEqual(result['send_budget_remaining'], 2)

    def test_file_upload_and_salary_basis_require_known_permission_and_evidence(self):
        self.configure()
        permissions = self.get('config/permissions.json')
        permissions['standing_application_authorization']['resume_refs']['headhunter'] = '.local/artifacts/cv.pdf'
        self.put('config/permissions.json', permissions)
        packet = self.packet()
        packet['resume_ref'] = '.local/artifacts/cv.pdf'
        self.assertIn('file_upload_authorization', self.build(packet)['missing'])
        settings = self.get('config/settings.json')
        settings['target']['salary'].update(minimum=100, currency='XYZ', period='month', basis='net')
        self.put('config/settings.json', settings)
        packet['vacancy']['salary'] = {'published': True, 'verified': True, 'max': 120,
                                      'currency': 'XYZ', 'period': 'month', 'basis': 'gross'}
        result = self.build(packet)
        self.assertEqual(result['eligibility']['decision'], 'needs_fact')


if __name__ == '__main__':
    unittest.main()
