import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import profile_rules

# Synthetic profile shared by tests that exercise the former-employer and private-note rules.
EXAMPLE = {
    'candidate': {'full_name': 'Morgan Sample', 'linkedin': 'linkedin.com/in/morgan-sample',
                  'email': 'morgan@example.com'},
    'former_employer': {
        'label': 'Example Corp',
        'names': ['example corp', 'example group'],
        'aliases': ['xc'],
        'flag': 'former_employer',
        'warning': 'Example Corp: check confidentiality and non-solicitation obligations before contact.',
    },
    'private_note_topics': ['medical', 'confidential', 'family matter', r'health\w*'],
}


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.rules = profile_rules.rules_from(EXAMPLE)

    def test_employer_names_match_with_flexible_spacing_and_case(self):
        for text in ('Example Corp', 'EXAMPLECORP', 'example  corp ltd', 'Example Group UK'):
            self.assertTrue(self.rules.at_employer(text), text)
        self.assertFalse(self.rules.at_employer('Another Co'))
        self.assertFalse(self.rules.at_employer(''))
        self.assertFalse(self.rules.at_employer(None))

    def test_leaver_needs_former_colleague_wording(self):
        self.assertTrue(self.rules.is_leaver('Former Example Corp colleague; confirmed'))
        self.assertTrue(self.rules.is_leaver('former xc colleague'))
        self.assertFalse(self.rules.is_leaver('Works at Example Corp'))

    def test_notes_mention_the_employer_by_name_or_short_form(self):
        for note in ('ex-XC', 'exxc', 'ex xc', 'former XC', 'XC colleague', 'met at Example Group'):
            self.assertTrue(self.rules.mentions_employer(note), note)
        for note in ('xcode user', 'nexcore', 'no link'):
            self.assertFalse(self.rules.mentions_employer(note), note)

    def test_private_topics_match_whole_words(self):
        self.assertTrue(self.rules.is_private('Talked about the medical results'))
        self.assertTrue(self.rules.is_private('healthcare news'))
        self.assertTrue(self.rules.is_private('A family matter'))
        self.assertFalse(self.rules.is_private('a confidentiality clause'))  # whole word only
        self.assertFalse(self.rules.is_private('weekly catch-up'))

    def test_flag_reads_list_and_legacy_boolean(self):
        self.assertTrue(self.rules.flagged({'flags': ['former_employer']}))
        self.assertTrue(self.rules.flagged({'former_employer': True}))
        self.assertFalse(self.rules.flagged({'flags': ['other']}))
        self.assertFalse(self.rules.flagged([]))

    def test_own_addresses_contain_a_part_of_the_users_name(self):
        for own in ('morgan@example.com', 'm.sample@work.example', 'Morgan Sample <hi@example.org>'):
            self.assertTrue(self.rules.is_own(own), own)
        for other in ('alex@example.com', '', None):
            self.assertFalse(self.rules.is_own(other), other)
        self.assertEqual(self.rules.own_linkedin, 'linkedin.com/in/morgan-sample')

    def test_empty_profile_matches_nothing(self):
        rules = profile_rules.rules_from({})
        self.assertEqual((rules.label, rules.flag, rules.warning), ('', '', ''))
        for check in (rules.at_employer, rules.is_leaver, rules.mentions_employer, rules.is_private, rules.is_own):
            self.assertFalse(check('anything at all'))
        self.assertFalse(rules.flagged({'flags': ['']}))

    def test_names_are_literal_not_patterns(self):
        rules = profile_rules.rules_from({'former_employer': {'names': ['a.b (c)']}})
        self.assertTrue(rules.at_employer('A.B (C) Ltd'))
        self.assertFalse(rules.at_employer('axb c'))


class LoadingTests(unittest.TestCase):
    def test_current_reads_the_profile_and_reloads_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profile.yml'
            path.write_text('former_employer: {label: First Ltd, names: [first ltd]}\n')
            with mock.patch.object(profile_rules, 'PROFILE', path), mock.patch.dict(profile_rules._cache, clear=True):
                self.assertEqual(profile_rules.current().label, 'First Ltd')
                path.write_text('former_employer: {label: Second Ltd, names: [second ltd]}\n')
                os.utime(path, ns=(1, 1))
                self.assertEqual(profile_rules.current().label, 'Second Ltd')

    def test_missing_profile_matches_nothing(self):
        with mock.patch.object(profile_rules, 'PROFILE', Path('/nonexistent/profile.yml')), \
                mock.patch.dict(profile_rules._cache, clear=True):
            self.assertFalse(profile_rules.current().at_employer('Example Corp'))

    def test_override_wins_and_is_removed_afterwards(self):
        before = profile_rules.current()
        with profile_rules.override(EXAMPLE) as rules:
            self.assertIs(profile_rules.current(), rules)
            self.assertEqual(rules.label, 'Example Corp')
        self.assertEqual(profile_rules.current(), before)

    def test_repository_profile_declares_a_complete_rule_and_private_topics(self):
        """The profile in this checkout must fill every field it uses, and never drop the private-topic
        screen: without topics, research context and notes would pass every line through."""
        rules = profile_rules.current()
        self.assertNotEqual(rules.private_topics.pattern, profile_rules.NEVER.pattern)
        if rules.employer.pattern != profile_rules.NEVER.pattern:
            self.assertTrue(rules.label and rules.flag and rules.warning)
            self.assertTrue(re.match(r'^[a-z_]+$', rules.flag))


if __name__ == '__main__':
    unittest.main()
