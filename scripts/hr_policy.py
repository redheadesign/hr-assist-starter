"""Resolve local grants and evidence-backed vacancy constraints; never send data."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from workspace import ROOT, local_path, read_json


def read_config(name):
    # Public defaults allow offline tests before setup; grants are always empty.
    return read_json(local_path('config/' + name), read_json(ROOT / 'templates' / name, {}))


def normalize_platform(value, channels=None):
    value = value.strip().casefold()
    platforms = (channels or read_config('channels.json'))['platforms']
    aliases = {alias.casefold(): name for name, info in platforms.items()
               for alias in info.get('aliases', [])}
    return aliases.get(value, value)


def valid_grant(grant):
    if not (grant.get('active') is True and grant.get('id') and grant.get('source') and grant.get('granted_on')):
        return False
    try:
        granted = datetime.fromisoformat(grant['granted_on'].replace('Z', '+00:00'))
        granted = granted.replace(tzinfo=timezone.utc) if granted.tzinfo is None else granted
        if granted > datetime.now(timezone.utc):
            return False
        expires = grant.get('expires_at')
        if expires:
            expiry = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                return False
    except (ValueError, TypeError):
        return False
    return True


def authorize(operation, platform):
    platform = normalize_platform(platform)
    policy = read_config('permissions.json')
    result = {'operation': operation, 'platform': platform, 'allowed': False,
              'grant_id': None, 'reason': 'requires_user_authorization'}
    reads = {normalize_platform(p): v for p, v in policy.get('read_authenticated_sources', {}).items()}
    if operation == 'monitor':
        result['allowed'] = reads.get(platform) is True
    elif operation in {'application', 'bot_reply'}:
        grant = policy.get('standing_application_authorization', {})
        covered = platform in {normalize_platform(p) for p in grant.get('platforms', [])}
        active = valid_grant(grant) and covered and policy.get('mode') != 'draft_only'
        if operation == 'application':
            result['allowed'] = active and policy.get('send_applications') is True
        else:
            bot = policy.get('bot_dialog_authorization', {})
            result['allowed'] = (active and valid_grant(bot) and bool(bot.get('allowed_topics'))
                                 and policy.get('reply_to_vacancy_bots') is True)
        result['grant_id'] = grant.get('id') if result['allowed'] else None
    elif operation == 'human_reply':
        # A global message switch is not a recipient- and purpose-specific grant.
        result['reason'] = 'check_specific_current_user_instruction'
    elif operation == 'profile_edit':
        result['reason'] = 'check_specific_profile_and_visibility_instruction'
    else:
        raise ValueError('Unknown operation: ' + operation)
    if result['allowed']:
        result['reason'] = 'local_user_grant_tool_rules_still_apply'
    return result


def eligibility(vacancy, search=None):
    settings = search if search is not None else read_config('settings.json')
    target = settings['target']
    rejected, missing = [], []
    if not target.get('roles'):
        missing.append('candidate_target_roles')
    for key in ('open', 'role_match', 'company_risk_checked', 'company_safe'):
        if vacancy.get(key) is False:
            rejected.append(key)
        elif vacancy.get(key) is not True:
            missing.append(key)
    for field, preference in (('work_format', 'formats'), ('employment', 'employment')):
        choices = target.get(preference, [])
        if not choices:
            missing.append('candidate_' + preference)
        elif vacancy.get(field) is None:
            missing.append(field)
        elif vacancy[field] not in choices:
            rejected.append(field)
    excluded = target.get('excluded_segments', [])
    if excluded and not vacancy.get('industry'):
        missing.append('industry')
    elif vacancy.get('industry') in excluded:
        rejected.append('industry')
    if vacancy.get('work_format') in ('hybrid', 'office'):
        if vacancy.get('location_match') is False:
            rejected.append('location')
        elif vacancy.get('location_match') is not True:
            missing.append('verified_location_and_relocation_match')
    test = vacancy.get('test_before_interview')
    if test is None:
        missing.append('test_before_interview')
    elif test is True and target.get('test_before_interview') is False:
        rejected.append('test_before_interview')
    elif test is True and target.get('test_before_interview') is not True:
        missing.append('candidate_test_policy')
    languages = vacancy.get('language_requirements')
    if languages is None:
        missing.append('language_requirements')
    else:
        levels = ['A1', 'A2', 'B1', 'B2', 'C1', 'C2']
        for language, level in languages.items():
            actual = target.get('languages', {}).get(language)
            if actual not in levels or level not in levels:
                missing.append('language_' + language)
            elif levels.index(actual) < levels.index(level):
                rejected.append('language_' + language)
    salary, expected = vacancy.get('salary') or {}, target.get('salary', {})
    floor = expected.get('minimum')
    if floor is not None and (salary.get('published') or salary.get('max') is not None):
        if (salary.get('verified') is not True or any(not expected.get(k) or salary.get(k) != expected[k]
                                                     for k in ('currency', 'period', 'basis'))):
            missing.append('comparable_verified_salary')
        elif salary.get('max') is not None and salary['max'] < floor:
            rejected.append('salary_below_floor')
    return {'decision': 'ineligible' if rejected else 'needs_fact' if missing else 'eligible',
            'reasons': rejected, 'missing_facts': sorted(set(missing)), 'score_cannot_override': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    auth = sub.add_parser('authorize')
    auth.add_argument('--operation', required=True, choices=['monitor', 'application', 'bot_reply', 'human_reply', 'profile_edit'])
    auth.add_argument('--platform', required=True)
    check = sub.add_parser('eligibility')
    check.add_argument('--json', dest='input_path', required=True)
    args = parser.parse_args()
    result = authorize(args.operation, args.platform) if args.command == 'authorize' else eligibility(
        json.loads(Path(args.input_path).read_text(encoding='utf-8')))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
