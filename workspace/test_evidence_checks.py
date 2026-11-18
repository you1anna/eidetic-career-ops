"""Advert availability: the daily source listing and research reads, combined by one rule."""
import datetime as dt
from pathlib import Path
import sys
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evidence_checks

NOW = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.timezone.utc)


def advert(**changes):
    record = dict(id='J1', type='job', source_lifecycle='active', published_deadline='',
                  advert={}, evidence_checks=dict(vacancy=None, contacts=[]))
    record.update(changes)
    return record


def listed(hours_ago, source='feed:adzuna:Adzuna UK — London'):
    return dict(source=source, observed_at=(NOW - dt.timedelta(hours=hours_ago)).isoformat())


def check(status, hours_ago, basis='Advert read.'):
    return dict(scope='vacancy', status=status, basis=basis, source_url='https://example.com/job',
                checked_at=(NOW - dt.timedelta(hours=hours_ago)).isoformat().replace('+00:00', 'Z'))


def availability(record):
    return evidence_checks.advert_availability(record, NOW)


class AdvertAvailabilityTests(unittest.TestCase):
    def test_a_listing_from_the_latest_daily_read_shows_the_advert_open(self):
        result = availability(advert(advert=listed(6)))
        self.assertEqual(result, dict(status='open', basis='listing', at=listed(6)['observed_at'],
                                      source='feed:adzuna', until=(NOW + dt.timedelta(hours=42)).isoformat()))

    def test_board_listing_names_its_provider_without_the_board_slug(self):
        result = availability(advert(advert=listed(2, 'board:greenhouse:gocardless')))
        self.assertEqual((result['status'], result['source']), ('open', 'board:greenhouse'))

    def test_a_listing_older_than_the_window_no_longer_confirms_it(self):
        result = availability(advert(advert=listed(49)))
        self.assertEqual((result['status'], result['basis'], result['source']), ('unconfirmed', 'listing', 'feed:adzuna'))
        self.assertEqual(result['at'], listed(49)['observed_at'])

    def test_the_window_is_forty_eight_hours(self):
        self.assertEqual(availability(advert(advert=listed(47.9)))['status'], 'open')
        self.assertEqual(availability(advert(advert=listed(48)))['status'], 'unconfirmed')

    def test_until_is_when_the_newest_evidence_lapses_and_is_set_only_while_open(self):
        record = advert(advert=listed(30), evidence_checks=dict(vacancy=check('confirmed', 10), contacts=[]))
        self.assertEqual(availability(record)['until'], (NOW + dt.timedelta(hours=38)).isoformat())
        lapses = evidence_checks.advert_availability(record, NOW + dt.timedelta(hours=38))
        self.assertEqual((lapses['status'], lapses['until']), ('unconfirmed', None))
        self.assertIsNone(availability(advert(advert=listed(1), source_lifecycle='removed'))['until'])

    def test_a_recent_confirmed_research_read_shows_it_open_without_a_listing(self):
        result = availability(advert(evidence_checks=dict(vacancy=check('confirmed', 30), contacts=[])))
        self.assertEqual((result['status'], result['basis'], result['source']), ('open', 'research', None))

    def test_a_confirmed_read_still_counts_when_the_listing_has_gone_stale(self):
        result = availability(advert(advert=listed(100), evidence_checks=dict(vacancy=check('confirmed', 3), contacts=[])))
        self.assertEqual((result['status'], result['basis'], result['source']), ('open', 'research', 'feed:adzuna'))

    def test_a_refused_or_failed_read_is_not_evidence_against_a_newer_or_older_listing(self):
        for hours in (1, 10):
            record = advert(advert=listed(6), evidence_checks=dict(vacancy=check('unverified', hours, 'refused'), contacts=[]))
            self.assertEqual(availability(record)['status'], 'open')
            self.assertEqual(availability(record)['basis'], 'listing')

    def test_no_listing_and_no_confirmation_is_unconfirmed_with_no_basis(self):
        result = availability(advert(evidence_checks=dict(vacancy=check('unverified', 2, 'robots_disallowed'), contacts=[])))
        self.assertEqual(result, dict(status='unconfirmed', basis=None, at=None, source=None, until=None))

    def test_source_closure_an_explicit_closed_read_or_a_passed_deadline_closes_it(self):
        cases = {
            'listing': advert(advert=listed(1), source_lifecycle='expired'),
            'research': advert(advert=listed(1), evidence_checks=dict(vacancy=check('closed', 20), contacts=[])),
            'deadline': advert(advert=listed(1), published_deadline='2026-10-26T17:00:00'),
        }
        for basis, record in cases.items():
            with self.subTest(basis=basis):
                result = availability(record)
                self.assertEqual((result['status'], result['basis']), ('closed', basis))

    def test_todays_london_deadline_has_not_passed(self):
        late = dt.datetime(2026, 10, 27, 23, 30, tzinfo=dt.timezone.utc)  # 23:30 GMT: still the 27th in London
        record = advert(advert=dict(source='feed:reed:x', observed_at=(late - dt.timedelta(hours=1)).isoformat()),
                        published_deadline='2026-10-27')
        self.assertEqual(evidence_checks.advert_availability(record, late)['status'], 'open')

    def test_research_scope_and_timeless_or_future_evidence_never_confirm(self):
        future = dict(source='feed:adzuna:x', observed_at=(NOW + dt.timedelta(hours=2)).isoformat())
        self.assertEqual(availability(advert(advert=future))['status'], 'unconfirmed')
        self.assertEqual(availability(advert(advert=dict(source='feed:adzuna:x')))['status'], 'unconfirmed')
        self.assertEqual(availability(advert(advert=dict(source='research:vps', observed_at=listed(1)['observed_at'])))['status'],
                         'unconfirmed')

    def test_gone_vocabulary_matches_the_relevance_configuration(self):
        config = yaml.safe_load((Path(__file__).resolve().parent.parent / 'config/relevance.yml').read_text())
        found = next(v['gone_lifecycles'] for v in config.values() if isinstance(v, dict) and 'gone_lifecycles' in v)
        self.assertEqual(set(evidence_checks.GONE_LIFECYCLES), set(found))


if __name__ == '__main__':
    unittest.main()
