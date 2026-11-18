"""The note's context pack: two-person LinkedIn threads only, private-topic messages held, research sections by name."""
import csv
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import note_context
import profile_rules
from test_profile_rules import EXAMPLE

OWN = 'https://www.linkedin.com/in/morgan-sample'
ALEX = 'https://www.linkedin.com/in/alex-example'
SAM = 'https://www.linkedin.com/in/sam-example'
FIELDS = ['CONVERSATION ID', 'CONVERSATION TITLE', 'FROM', 'SENDER PROFILE URL', 'TO', 'RECIPIENT PROFILE URLS',
          'DATE', 'SUBJECT', 'CONTENT', 'FOLDER', 'ATTACHMENTS', 'IS MESSAGE DRAFT']


def message(conv, sender, to, date, content):
    names = {OWN: 'Morgan Sample', ALEX: 'Alex Example', SAM: 'Sam Example'}
    return {'CONVERSATION ID': conv, 'FROM': names[sender], 'SENDER PROFILE URL': sender, 'TO': '',
            'RECIPIENT PROFILE URLS': ','.join(to), 'DATE': date, 'CONTENT': content}


class NoteContextTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(profile_rules.override(EXAMPLE))
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.export = root / 'export'; self.export.mkdir()
        self.research = root / 'research'; self.research.mkdir()
        with (self.export / 'messages.csv').open('w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, FIELDS, restval='')
            w.writeheader()
            w.writerows([
                message('c1', ALEX, [OWN], '2026-02-04 09:00:00 UTC', 'Happy to introduce you to the QE lead.'),
                message('c1', OWN, [ALEX + '/'], '2026-02-04 10:00:00 UTC', 'Thanks, that would be great.'),
                message('c1', ALEX, [OWN], '2026-02-05 10:00:00 UTC', 'Keep the medical news to yourself.'),
                message('c1', OWN, [ALEX], '2026-02-06 10:00:00 UTC', 'The confidential offer moved to May.'),
                message('c2', ALEX, [OWN, SAM], '2026-03-01 10:00:00 UTC', 'A group plan for Friday.'),
                message('c3', SAM, [OWN], '2026-03-02 10:00:00 UTC', 'Hello from Sam.'),
            ])
        (self.research / 'notes.md').write_text('# Notes\n\n## Alex Example\n- Met at the March event.\n\n'
                                                '## Sam Example\n- Not about Alex.\n', encoding='utf-8')
        self.show = dict(id='CP-1', name='Alex Example', url=ALEX, relationship='active', kind='former_colleague',
                         preferred_channel='linkedin', preferred_ask='catch_up', known_context='Your note: worked together',
                         interactions=[dict(at='2026-02-04', channel='linkedin', direction='out', summary='Sent')],
                         draft=dict(purpose='message', version_id=3, actor='agent', text='Hi Alex'))

    def tearDown(self):
        self.tmp.cleanup()

    def pack(self):
        out = io.StringIO()
        with redirect_stdout(out), tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(self.show, f)
            f.flush()
            note_context.main([f.name, '--export', str(self.export), '--research', str(self.research)])
        return out.getvalue()

    def test_only_threads_with_them_alone_are_read_and_legal_messages_are_held(self):
        text = self.pack()
        self.assertIn('Happy to introduce you to the QE lead.', text)
        self.assertIn('Thanks, that would be great.', text)
        self.assertNotIn('medical news', text)
        self.assertNotIn('confidential offer', text)  # held in the thread and in the user's own voice
        self.assertIn('[held: private topic]', text)
        self.assertNotIn('A group plan', text)
        self.assertNotIn('Hello from Sam', text)

    def test_card_facts_and_research_sections_that_name_them(self):
        text = self.pack()
        self.assertIn('Your note: worked together', text)
        self.assertIn('Current note (message, version 3, by agent)', text)
        self.assertIn('Met at the March event.', text)
        self.assertNotIn('Not about Alex', text)

    def test_nested_research_and_correspondence_headers_respect_legal_screen(self):
        corr = self.research / 'correspondence' / '2026-10-16'
        raw = corr / 'raw'
        raw.mkdir(parents=True)
        (corr.parent / 'legal-domains.json').write_text(json.dumps({'domains': ['lawfirm.example']}), encoding='utf-8')
        (raw / 'legal-00.json').write_text(json.dumps([{'threadId': 'held-thread'}]), encoding='utf-8')
        (corr / 'people.json').write_text(json.dumps([
            {'id': 'CP-1', 'name': 'Alex Example', 'emails': ['alex@acme.example'],
             'direct_count': 1, 'last_direct': '2025-11-10', 'last_direction': 'in',
             'group_count': 2, 'last_group': '2024-05-01'}
        ]), encoding='utf-8')
        (raw / 'mail-00.json').write_text(json.dumps([
            {'threadId': 't1', 'date': '2025-11-10T10:00:00Z', 'direction': 'in',
             'headers': {'From': 'Alex Example <alex@acme.example>', 'To': 'morgan@example.com', 'Subject': 'Coffee next week'}},
            {'threadId': 't2', 'date': '2024-05-01T10:00:00Z', 'direction': 'in',
             'headers': {'From': 'Alex Example <alex@acme.example>', 'To': 'morgan@example.com, pat@sharedcorp.example',
                         'Subject': 'Team dinner'}},
            {'threadId': 'held-thread', 'date': '2026-03-01T10:00:00Z', 'direction': 'in',
             'headers': {'From': 'Alex Example <alex@acme.example>', 'To': 'morgan@example.com', 'Subject': 'Confidential thread'}},
            {'threadId': 't3', 'date': '2026-03-02T10:00:00Z', 'direction': 'in',
             'headers': {'From': 'Alex Example <alex@lawfirm.example>', 'To': 'morgan@example.com', 'Subject': 'Legal advice'}},
            {'threadId': 't4', 'date': '2026-03-03T10:00:00Z', 'direction': 'in',
             'headers': {'From': 'Alex Example <alex@acme.example>', 'To': 'morgan@example.com', 'Subject': 'Medical results'}},
        ]), encoding='utf-8')
        (corr / 'mining.md').write_text('# Mining\n\n## Enriched\n- Alex Example: overlapped at SharedCorp\n', encoding='utf-8')
        text = self.pack()
        self.assertIn('alex@acme.example', text)
        self.assertIn('Coffee next week', text)
        self.assertIn('sharedcorp.example', text)
        self.assertIn('overlapped at SharedCorp', text)
        self.assertNotIn('Confidential thread', text)
        self.assertNotIn('Legal advice', text)
        self.assertNotIn('Medical results', text)  # a private topic in the header holds the message

    def test_lint_note_flags_canned_templates_dashes_and_qe_pigeonholing(self):
        pigeonholed = (
            "Hi Alex, hope you're good, and that Acme is treating you well.\n\n"
            "News from me: I left in May — looking for a Head of QE or Test Manager role."
        )
        warnings = note_context.lint_note(pigeonholed, role='CTO', kind='former_colleague')
        joined = '\n'.join(warnings)
        self.assertIn('News from me:', joined)
        self.assertIn('treating you well', joined)
        self.assertIn('em/en dash', joined)
        self.assertIn('Positioning:', joined)

        balanced = (
            "Hi Alex, it has been ages! Since the summer I have been building AI and systems engineering "
            "projects, and I am open to Engineering Manager, Head of QE, Forward Deployed or Head of "
            "Engineering roles. More at portfolio.example.com"
        )
        self.assertEqual(note_context.lint_note(balanced, role='CTO', kind='former_colleague'), [])
        cvs = '\n'.join(note_context.cv_positioning())
        for key in ('Director', 'Manager', 'FDE', 'Founding', 'QE', 'QELead'):
            self.assertIn(f'- {key} (', cvs)


    def test_every_first_approach_links_to_the_site_but_follow_ups_need_not(self):
        missing = 'No link to portfolio.example.com: every first approach includes it.'
        bare = "Hi Sam,\n\nWe last spoke at the roundtable.\n\nWould you be up for a call?\n\nThanks,\nMorgan"
        for tid in ('warm', 'recruiter', 'founder', ''):
            with self.subTest(template=tid):
                self.assertIn(missing, note_context.lint_note(bare, template=tid))
                self.assertNotIn(missing, note_context.lint_note(bare.replace('call?', 'call? More at portfolio.example.com'), template=tid))
        for tid in ('bump', 'nothing_now', 'intro_thanks', 'update'):
            self.assertNotIn(missing, note_context.lint_note(bare, template=tid))
        self.assertNotIn(missing, note_context.lint_note(bare, template='warm', first=False))
        pack = note_context.template_pack()
        for t in pack['templates']:
            for text in filter(None, (t['body'], t.get('short'))):
                self.assertIn('portfolio.example.com', text, t['id'])  # the pack itself never trips the check

    def test_pack_checks_match_the_site_note_box(self):
        long_warm = 'Hi Sam,\n\n' + ' '.join(['word'] * 159) + ' portfolio.example.com\n\nThanks,\nMorgan'
        issues = note_context.lint_note(long_warm, template='warm')
        self.assertIn('About 164 words: the pack keeps these under 150.', issues)
        self.assertEqual(note_context.lint_note(long_warm, template='recruiter'), [])  # recruiters can run longer
        noted = ("Subject: Catching up\n\nHi Sam,\n\nHope you're well.\n\n{{personal_line}} I'm passionate about "
                 "robust QA. See https://www.google.com/url?q=x and portfolio.example.com\n\nMorgan")
        joined = '\n'.join(note_context.lint_note(noted, template='warm'))
        for expected in ('Gaps still to fill: personal_line', 'Don\'t say "passionate" or "robust"', 'Don\'t say "QA"',
                         'not "Hope you\'re well" on its own', '2 links', 'Google redirect'):
            self.assertIn(expected, joined)
        bullets = 'Hi Riley,\n\nQuick summary:\n- Head of QE desk, ideally on an AI product.\n\nMorgan'
        self.assertTrue(any(i.startswith('Formatting') for i in note_context.lint_note(bullets, template='warm')))
        self.assertFalse(any(i.startswith(('Formatting', 'Positioning')) for i in note_context.lint_note(bullets, template='recruiter')))

    def test_template_the_site_starts_from(self):
        pack = note_context.template_pack()
        cases = [(dict(kind='former_colleague', preferred_ask='catch_up'), 'warm'),
                 (dict(kind='former_colleague'), 'warm'),  # no ask set means a catch-up
                 (dict(kind='former_colleague', preferred_ask='introduction'), 'former_colleague'),
                 (dict(kind='introducer', preferred_ask='introduction'), 'connector'),
                 (dict(kind='former_manager', preferred_ask='catch_up'), 'former_manager'),
                 (dict(kind='recruiter', preferred_ask='availability'), 'recruiter'),
                 (dict(kind='friend', preferred_ask='catch_up'), 'friend'),
                 (dict(relationship='dormant'), 'warm'), (dict(relationship='cold'), 'weak_tie'), (dict(), 'weak_tie')]
        for show, expected in cases:
            with self.subTest(show=show):
                self.assertEqual(note_context.suggest_template(show, pack), expected)


if __name__ == '__main__':
    unittest.main()
