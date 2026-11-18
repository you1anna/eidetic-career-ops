"""The server uses only the existing ChatGPT CLI identity and bounded structured output."""
import json
import unittest
from unittest.mock import patch
import research_codex as codex

class CodexBoundaryTests(unittest.TestCase):
    def test_command_is_scoped_read_only_and_chatgpt_only(self):
        args=codex.command('/tmp/work','/tmp/schema','/tmp/result',False)
        self.assertIn('--ignore-user-config',args)
        self.assertIn('--ephemeral',args)
        self.assertIn('read-only',args)
        self.assertIn('forced_login_method="chatgpt"',args)
        self.assertIn('approval_policy="never"',args)
        self.assertIn('web_search="disabled"',args)
        for name in ('shell_tool','apps','plugins','hooks','multi_agent','computer_use','browser_use'):
            self.assertIn(name,args)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',args)
        self.assertNotIn('OPENAI_API_KEY',codex.environment())
        self.assertNotIn('CODEX_API_KEY',codex.environment())

    def test_search_is_explicit_and_cannot_select_arbitrary_commands(self):
        args=codex.command('/tmp/work','/tmp/schema','/tmp/result',True)
        self.assertIn('web_search="live"',args)
        with self.assertRaises(ValueError):codex.validate_request({'operation':'shell','prompt':{}})
        with self.assertRaises(ValueError):codex.validate_request({'operation':'finding','prompt':{'text':'x'*49000}})

    def test_invalid_structured_findings_and_extra_fields_are_rejected(self):
        schema={'type':'object','properties':{'urls':{'type':'array','items':{'type':'string'},'maxItems':4}},'required':['urls'],'additionalProperties':False}
        codex.validate_output({'urls':['https://example.com']},schema)
        for value in ({'urls':[1]},{'urls':[],'shell':'anything'},{'urls':['x']*5},{}):
            with self.subTest(value=value),self.assertRaises(ValueError):codex.validate_output(value,schema)

    def test_auth_usage_limit_and_crash_are_distinct_without_raw_provider_text(self):
        self.assertEqual(codex.failure('usage limit reached; private context'),'usage_limit')
        self.assertEqual(codex.failure('refresh token expired; private token'),'access')
        self.assertEqual(codex.failure('other failure; private context'),'failed')

    def test_unapproved_login_type_cannot_start_a_model_call(self):
        with patch.object(codex.subprocess,'run') as run:
            run.return_value.returncode=0;run.return_value.stdout='Logged in using an API key';run.return_value.stderr=''
            with self.assertRaises(codex.LoginRequired):codex.check_login()
        self.assertEqual(run.call_count,1)

class ContactScopeTests(unittest.TestCase):
    def test_failed_contact_read_keeps_its_exact_organisation_scope(self):
        import sys
        from pathlib import Path
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"workspace"))
        import research_sources
        person={'id':'CP-person','name':'Alex Example','url':'https://example.com/team','organisation_id':'CO-employer'}
        job={'name':'AI Engineer','organisation':'Example Labs','url':'https://example.com/job'}
        result=research_sources.check_claim(job,{'source':0},[{'url':person['url'],'checked_at':'2026-10-17T12:00:00Z','ok':False,'reason':'robots_disallowed'}],person)
        self.assertEqual(result['organisation_id'],'CO-employer')
        self.assertEqual(result['status'],'unverified')


class PreparationOperationTests(unittest.TestCase):
    def test_runner_rejects_retired_preparation_and_unknown_operations(self):
        import research_codex
        with self.assertRaisesRegex(ValueError, 'retired'):
            research_codex.validate_request({'operation': 'preparation', 'prompt': {'packet': {}}})
        with self.assertRaises(ValueError):
            research_codex.validate_request({'operation': 'improvement', 'prompt': {}})
        self.assertIn('--disable', research_codex.command('/tmp/w', '/tmp/s', '/tmp/r', False))
        self.assertIn('web_search="disabled"', research_codex.command('/tmp/w', '/tmp/s', '/tmp/r', False))
