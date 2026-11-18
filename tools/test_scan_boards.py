import base64
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_boards as sb  # noqa: E402


class RipplingTests(unittest.TestCase):
    def test_partial_or_invalid_reads_do_not_close_previously_observed_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = root / 'portals.yml'
            cfg.write_text('title_filter:\n  positive: [Quality Engineer]\ntracked_companies:\n'
                           '  - {name: Example, careers_url: https://ats.rippling.com/example/jobs}\n')
            fx = root / 'rippling-example.json'
            fx.write_text(json.dumps(self.payload()))
            first = sb.run(cfg, root / 'out', '2026-10-01', root, feeds=False)
            self.assertEqual(first['boards']['Example']['state'], 'ok')
            self.assertEqual(first['boards']['Example']['new'], 1)
            for payload, expected in ((dict(self.payload(21), items=[]), 'partial'), ({'items': []}, 'error'),
                                      (dict(self.payload(), items=[], totalItems=0, pageSize=0, totalPages=0), 'error'),
                                      (dict(self.payload(), totalPages=0), 'error'),
                                      (dict(self.payload(), page=1), 'error')):
                fx.write_text(json.dumps(payload))
                status = sb.run(cfg, root / 'out', '2026-10-02', root, feeds=False)
                self.assertEqual(status['boards']['Example']['state'], expected)
                item, = json.loads((root / 'out/pending/observations.json').read_text())['items'].values()
                self.assertEqual(item['source_lifecycle'], 'active')
                self.assertEqual(item['absence_observations'], [])

    def payload(self, total=1):
        return {'items': [{'id': '11111111-2222-4333-8444-555555555555',
                 'name': 'Principal QA Engineer',
                 'url': 'https://ats.rippling.com/example/jobs/11111111-2222-4333-8444-555555555555',
                 'department': {'name': 'Engineering'},
                 'locations': [{'name': 'London, United Kingdom', 'workplaceType': 'ON_SITE'}]}],
                'page': 0, 'pageSize': 20, 'totalItems': total, 'totalPages': 1 if total <= 20 else 2}

    def html(self, payload):
        return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(
            {'props': {'pageProps': {'dehydratedState': {'queries': [
                {'queryKey': ['board', 'example', 'locations'], 'state': {'data': {'items': []}}},
                {'queryKey': ['board', 'example', 'job-posts', False, {'page': 0}],
                 'state': {'data': payload}}]}}}}) + '</script>'

    def test_one_public_board_read_preserves_sparse_terms(self):
        self.assertEqual(sb.detect('https://ats.rippling.com/example/jobs'), ('rippling', 'example'))
        with patch.object(sb, 'fetch_text', return_value=self.html(self.payload())) as fetch:
            payload = sb.fetch_board('rippling', 'example')
        fetch.assert_called_once_with('https://ats.rippling.com/example/jobs')
        job, = sb.normalise('rippling', payload)
        self.assertEqual((job['title'], job['location'], job['workplace']),
                         ('Principal QA Engineer', 'London, United Kingdom', 'onsite'))
        self.assertEqual((job['posted'], job['text'], job['employment_type']), ('', '', ''))
        self.assertTrue(sb.rippling_complete(payload))
        self.assertFalse(sb.rippling_complete(self.payload(21)))

    def test_missing_or_malformed_board_is_an_error_not_empty_success(self):
        for html in ('<html>challenge</html>', self.html({'items': []}),
                     self.html(dict(self.payload(), items=[{'name': 'QA Lead'}]))):
            with self.subTest(html=html[:30]), patch.object(sb, 'fetch_text', return_value=html):
                with self.assertRaises(ValueError):
                    sb.fetch_board('rippling', 'example')
        bad = self.payload()
        bad['items'][0]['url'] = 'https://evil.test/jobs/1'
        with patch.object(sb, 'fetch_text', return_value=self.html(bad)):
            with self.assertRaises(ValueError):
                sb.fetch_board('rippling', 'example')

    def test_filtered_first_page_cannot_claim_complete_board_coverage(self):
        html = self.html(self.payload()).replace('"page": 0}', '"page": 0, "searchQuery": "QA"}')
        with patch.object(sb, 'fetch_text', return_value=html):
            with self.assertRaises(ValueError):
                sb.fetch_board('rippling', 'example')

class SearchBalanceTests(unittest.TestCase):
    def test_live_search_config_covers_three_priorities_without_expanding_call_budget(self):
        import yaml
        from urllib.parse import parse_qs, urlparse
        import job_rules
        cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / 'config/portals.yml').read_text())
        remote, main, reed = [f['params'] for f in cfg['feeds']]
        self.assertEqual(len(main['phrases']), 15)
        self.assertEqual(len(set(main['phrases'])), 15)
        self.assertEqual(reed['phrases'], main['phrases'])
        self.assertEqual(len(remote['phrases']), 6)
        # Independent representative contracts: actual requests must reach each new work family.
        for phrase in ('head of engineering', 'AI engineer', 'head of AI', 'forward deployed engineer',
                       'integration engineer', 'developer productivity', 'head of delivery', 'QA lead'):
            with self.subTest(phrase=phrase), patch.dict(os.environ, {'ADZUNA_APP_ID':'fake','ADZUNA_APP_KEY':'fake'}):
                self.assertIn(phrase, main['phrases'])
                url, _ = sb.feed_request('adzuna', main, 1, phrase=phrase)
                self.assertEqual(parse_qs(urlparse(url).query)['what_phrase'], [phrase])
                self.assertTrue(job_rules.title_match(phrase, cfg['title_filter'])[0])
        self.assertEqual(main['phrases'][:3], ['head of engineering','AI engineer','integration engineer'])
        self.assertTrue({'AI engineer','head of AI','integration engineer','developer productivity'} <= set(remote['phrases']))
        self.assertNotIn('feed_limits', cfg)
        self.assertEqual(sb.feed_limits(), {'adzuna': {'max_calls':10,'min_interval_seconds':4.1},
                                          'reed': {'max_calls':10,'min_interval_seconds':2.6}})
        self.assertNotIn('reed_detail_budget', cfg)
        self.assertEqual(sb.collection_policy.load()['providers']['reed']['detail_limit'], 2)
        self.assertEqual([p['max_pages'] for p in (main,remote,reed)], [1,1,1])
        self.assertFalse(any(p.get('contract') or p.get('permanent') for p in (main,remote,reed)))

PORTALS = """
title_filter:
  positive: ["Head of Engineering", "Test Manager", "Engineering Manager", "Evaluation", "CTO", "Forward Deployed", "Quality Engineering", "Test Lead"]
  negative: ["Junior", "Clearance", "Marketing", "Internship"]
engagement_filter:
  text_markers: ["day rate", "per day", "outside ir35", "month contract"]
  interim_markers: ["interim", "fractional"]
  minimum_day_rate_gbp: 500
  prefer: ["contract", "interim", "fractional", "ftc"]
location_filter:
  accept_any_of: ["london", "remote", "uk"]
  reject_any_of: ["san francisco", "singapore"]
tracked_companies:
  - {name: GH Co, careers_url: https://job-boards.greenhouse.io/ghco, enabled: true}
  - {name: Lever Co, careers_url: https://jobs.lever.co/leverco, enabled: true}
  - {name: Ashby Co, careers_url: https://jobs.ashbyhq.com/ashbyco, enabled: true}
  - {name: SR Co, careers_url: https://jobs.smartrecruiters.com/SRCo, enabled: true}
  - {name: Missing Co, careers_url: https://jobs.ashbyhq.com/nope, enabled: true}
  - {name: Off Co, careers_url: https://jobs.ashbyhq.com/off, enabled: false}
  - {name: Odd Co, careers_url: https://example.com/careers, enabled: true}
"""

GH = {"jobs": [
    {"title": "Interim Head of Engineering", "location": {"name": "London"}, "absolute_url": "https://gh/1", "updated_at": "2026-09-10T00:00:00Z", "content": "<p>6 month contract, £800 per day outside IR35</p>"},
    {"title": "Junior Test Manager", "location": {"name": "London"}, "absolute_url": "https://gh/2", "content": ""},
    {"title": "Test Manager", "location": {"name": "San Francisco"}, "absolute_url": "https://gh/3", "content": ""},
    {"title": "Test Manager", "location": {"name": "Remote - UK"}, "absolute_url": "https://gh/4", "content": "Contract, £400/day"},
    {"title": "Engineering Manager", "location": {"name": "London"}, "absolute_url": "https://gh/5", "content": "Permanent role",
     "pay_input_ranges": [{"min_cents": 11000000, "max_cents": 13000000, "currency_type": "GBP", "title": "Annual Salary"}],
     "departments": [{"name": "Engineering"}], "metadata": [{"name": "Location Type", "value": "Hybrid"}]},
    {"title": "Director of Marketing", "location": {"name": "London"}, "absolute_url": "https://gh/6", "content": "B2B SaaS, employment contract"},
    {"title": "Forward Deployed Engineer", "location": {"name": "Washington, DC"}, "absolute_url": "https://gh/7", "content": ""},
    {"title": "Head of Engineering", "location": {"name": "Sunnyvale"}, "absolute_url": "https://gh/8", "content": ""},
    {"title": "Head of Engineering", "location": {"name": "New York, NY"}, "absolute_url": "https://gh/9", "content": "Full-time"},
    {"title": "Head of Engineering", "location": {"name": "San Francisco, CA | New York City, NY"}, "absolute_url": "https://gh/10", "content": ""},
    {"title": "Head of Engineering", "location": {"name": "United States (Remote)"}, "absolute_url": "https://gh/11", "content": ""},
    {"title": "Engineering Manager", "location": {"name": "London / Internship"}, "absolute_url": "https://gh/12", "content": ""},
    {"title": "Test Lead", "location": {"name": "London"}, "absolute_url": "https://gh/13", "content": "Contract role",
     "pay_input_ranges": [{"min_cents": 30000, "max_cents": 40000, "currency_type": "GBP", "title": "Daily rate"}]},
]}
LEVER = [
    {"text": "Engineering Manager, Platform", "categories": {"location": "London", "commitment": "Full-time", "department": "Engineering"}, "hostedUrl": "https://lv/1", "createdAt": 1757462400000, "descriptionPlain": "Full-time permanent.",
     "salaryRange": {"currency": "GBP", "interval": "per-year-salary", "min": 90000, "max": 110000}, "workplaceType": "hybrid"},
]
ASHBY = {"jobs": [
    {"title": "AI Evaluation Lead", "location": "Remote", "isRemote": True, "jobUrl": "https://ab/1", "publishedAt": "2026-09-12T00:00:00Z", "descriptionPlain": "Contract, outside IR35", "employmentType": "Contract",
     "workplaceType": "Remote", "department": "Engineering",
     "compensation": {"compensationTierSummary": "£650 – £750 / day",
                      "summaryComponents": [{"compensationType": "Salary", "interval": "1 DAY", "currencyCode": "GBP", "minValue": 650, "maxValue": 750},
                                            {"compensationType": "EquityCashValue", "interval": "1 YEAR", "currencyCode": "GBP", "minValue": None, "maxValue": None}]}},
]}
SMARTRECRUITERS = {"offset": 0, "limit": 100, "totalFound": 1, "content": [
    {"id": "7001", "name": "Head of Quality Engineering", "company": {"identifier": "SRCo", "name": "SR Co"}, "releasedDate": "2026-09-11T08:00:00.000Z",
     "location": {"city": "London", "country": "gb", "remote": False, "hybrid": True, "fullLocation": "London, England, United Kingdom"},
     "typeOfEmployment": {"id": "contract", "label": "Contract"}, "department": {"id": "1", "label": "Technology"}},
]}


def read_rows(path: Path) -> list[dict]:
    lines = path.read_text().splitlines()
    cols = lines[0].split("\t")
    return [dict(zip(cols, line.split("\t"))) for line in lines[1:]]


class ScanTests(unittest.TestCase):
    def setUp(self):
        import collection_schedule
        session=collection_schedule.Session
        self.schedule_patch=patch.object(collection_schedule,'Session',side_effect=lambda data,offline=False:session(data,offline=True))
        self.schedule_patch.start();self.addCleanup(self.schedule_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.portals = root / "portals.yml"
        self.portals.write_text(PORTALS)
        self.fx = root / "fx"
        self.fx.mkdir()
        (self.fx / "greenhouse-ghco.json").write_text(json.dumps(GH))
        (self.fx / "lever-leverco.json").write_text(json.dumps(LEVER))
        (self.fx / "ashby-ashbyco.json").write_text(json.dumps(ASHBY))
        (self.fx / "smartrecruiters-SRCo.json").write_text(json.dumps(SMARTRECRUITERS))
        self.out = root / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ashby_explicit_workplace_wins_over_remote_boolean(self):
        for workplace, remote, expected_location, expected_workplace in (
            ('Hybrid', True, 'London', 'hybrid'),
            ('OnSite', True, 'London', 'onsite'),
            ('Remote', False, 'London / Remote', 'remote'),
            ('', True, 'London / Remote', 'remote'),
            (None, True, 'London / Remote', 'remote'),
            ('', False, 'London', ''),
        ):
            with self.subTest(workplace=workplace, remote=remote):
                source = dict(title='Engineering Manager', location='London', workplaceType=workplace,
                              isRemote=remote, jobUrl='https://example.test/jobs/manager')
                job, = sb.normalise('ashby', {'jobs': [source]})
                self.assertEqual((job['location'], job['workplace']), (expected_location, expected_workplace))
                self.assertEqual(source['location'], 'London')

    def test_ashby_workplace_correction_reaches_observations_and_tsv(self):
        payload = {'jobs': [dict(title='Engineering Manager', location='London', workplaceType='Hybrid',
                               isRemote=True, jobUrl='https://example.test/jobs/manager',
                               descriptionPlain='Lead software engineering teams.', employmentType='FullTime')]}
        (self.fx / 'ashby-ashbyco.json').write_text(json.dumps(payload))
        sb.run(self.portals, self.out, '2026-09-14', self.fx, feeds=False)
        row = next(r for r in read_rows(self.out / 'pending/2026-09-14.tsv')
                   if r['url'] == 'https://example.test/jobs/manager')
        self.assertEqual((row['location'], row['workplace']), ('London', 'hybrid'))
        stored = json.loads((self.out / 'pending/observations.json').read_text())['items']
        current = next(item['current'] for item in stored.values()
                       if item['current'].get('url') == 'https://example.test/jobs/manager')
        self.assertEqual((current['location'], current['workplace']), ('London', 'hybrid'))

    def test_a_board_behind_a_bot_challenge_is_recorded_as_challenged(self):
        from email.message import Message
        headers = Message()
        headers["cf-mitigated"] = "challenge"
        challenge = urllib.error.HTTPError("https://api.lever.co/v0/postings/leverco", 403, "Forbidden", headers, None)
        with patch.object(sb, "fetch_board", side_effect=challenge):
            sb.run(self.portals, self.out, "2026-09-24")
        sources = json.loads((self.out / "pending" / "observations.json").read_text())["sources"]
        detail = sources["board:lever:leverco"]["detail"]
        self.assertEqual((sources["board:lever:leverco"]["state"], detail["kind"], detail["status"]),
                         ("http-403", "challenged", 403))

    def test_detect(self):
        self.assertEqual(sb.detect("https://job-boards.greenhouse.io/anthropic"), ("greenhouse", "anthropic"))
        self.assertEqual(sb.detect("https://boards.greenhouse.io/x"), ("greenhouse", "x"))
        self.assertEqual(sb.detect("https://jobs.lever.co/palantir"), ("lever", "palantir"))
        self.assertEqual(sb.detect("https://jobs.ashbyhq.com/runna?x=1"), ("ashby", "runna"))
        self.assertEqual(sb.detect("https://jobs.smartrecruiters.com/Lyst"), ("smartrecruiters", "Lyst"))
        self.assertEqual(sb.detect("https://example.com/careers"), (None, None))
        self.assertIn("pay_transparency=true", sb.api_url("greenhouse", "x"))

    def test_rules_and_output(self):
        status = sb.run(self.portals, self.out, "2026-09-14", self.fx)
        rows = (self.out / "pending" / "2026-09-14.tsv").read_text().splitlines()
        self.assertEqual(rows[0].split("\t"), sb.COLUMNS)
        body = [r.split("\t") for r in rows[1:]]
        urls = [r[7] for r in body]
        by_url = {r[7]: r for r in body}
        # kept: gh/1 (interim, £800), gh/5 (permanent, unstated rate), lv/1, ab/1
        self.assertIn("https://gh/1", urls)
        self.assertIn("https://gh/5", urls)
        self.assertIn("https://lv/1", urls)
        self.assertIn("https://ab/1", urls)
        # dropped: gh/2 negative title, gh/3 rejected location, gh/4 below £500 floor,
        # gh/6 'Director' must not match 'CTO' and Marketing is negative, gh/7 DC and gh/8 Sunnyvale are US,
        # gh/13 structured day rate below the floor
        for u in ("https://gh/2", "https://gh/3", "https://gh/4", "https://gh/6", "https://gh/7", "https://gh/8", "https://gh/10", "https://gh/11", "https://gh/12", "https://gh/13"):
            self.assertNotIn(u, urls)
        self.assertNotIn("https://gh/9", urls)  # timezone compatibility does not grant US residence
        # contract-first ordering: preferred engagements before permanent
        engagements = [r[3] for r in body]
        first_perm = engagements.index("permanent")
        self.assertTrue(all(e != "permanent" for e in engagements[:first_perm]))
        self.assertEqual(by_url["https://gh/1"][3], "interim")
        self.assertEqual(by_url["https://gh/1"][4], "£800/day")
        self.assertEqual(by_url["https://ab/1"][3], "contract")
        self.assertEqual(by_url["https://ab/1"][4], "£650–750/day")
        self.assertEqual(by_url["https://lv/1"][3], "permanent")
        # board status
        self.assertEqual(status["boards"]["Missing Co"]["state"], "bad-slug")
        self.assertEqual(status["boards"]["Odd Co"]["state"], "unsupported-url")
        self.assertNotIn("Off Co", status["boards"])
        self.assertEqual(status["totals"]["new"], 5)
        self.assertEqual(status["totals"]["preferred_new"], 3)

    def test_structured_pay_and_terms(self):
        sb.run(self.portals, self.out, "2026-09-14", self.fx)
        by_url = {r["url"]: r for r in read_rows(self.out / "pending" / "2026-09-14.tsv")}
        gh5 = by_url["https://gh/5"]
        self.assertEqual((gh5["pay_min"], gh5["pay_max"], gh5["pay_currency"], gh5["pay_period"], gh5["pay_source"]), ("110000", "130000", "GBP", "year", "ats"))
        self.assertEqual((gh5["workplace"], gh5["department"]), ("hybrid", "Engineering"))
        self.assertEqual(gh5["rate"], "GBP 110000–130000/year")
        lv1 = by_url["https://lv/1"]
        self.assertEqual((lv1["pay_min"], lv1["pay_max"], lv1["pay_period"], lv1["workplace"], lv1["department"], lv1["employment_type"]), ("90000", "110000", "year", "hybrid", "Engineering", "Full-time"))
        ab1 = by_url["https://ab/1"]
        self.assertEqual((ab1["pay_min"], ab1["pay_max"], ab1["pay_period"], ab1["workplace"], ab1["ir35"]), ("650", "750", "day", "remote", "outside"))
        gh1 = by_url["https://gh/1"]
        self.assertEqual((gh1["pay_min"], gh1["pay_max"], gh1["pay_period"], gh1["pay_source"], gh1["ir35"]), ("800", "800", "day", "text", "outside"))
        sr = by_url["https://jobs.smartrecruiters.com/SRCo/7001"]
        self.assertEqual((sr["company"], sr["engagement"], sr["workplace"], sr["department"], sr["posted"], sr["provider"]), ("SR Co", "contract", "hybrid", "Technology", "2026-09-11", "smartrecruiters"))

    def test_observations_store_normalized_terms_and_clear_removed_facts(self):
        sb.run(self.portals, self.out, "2026-09-14", self.fx)
        observations = json.loads((self.out / "pending/observations.json").read_text())
        key = "board:greenhouse:ghco\x1fhttps://gh/1"
        current = observations["items"][key]["current"]
        self.assertEqual(current["company"], "GH Co")
        self.assertEqual((current["engagement"], current["rate"], current["pay_min"],
                          current["pay_max"], current["pay_currency"], current["pay_period"],
                          current["pay_source"], current["ir35"]),
                         ("interim", "£800/day", "800", "800", "GBP", "day", "text", "outside"))

        refreshed = json.loads(json.dumps(GH))
        refreshed["jobs"][0]["title"] = "Head of Engineering"
        refreshed["jobs"][0]["content"] = "Terms to be discussed."
        (self.fx / "greenhouse-ghco.json").write_text(json.dumps(refreshed))
        sb.run(self.portals, self.out, "2026-09-15", self.fx)
        current = json.loads((self.out / "pending/observations.json").read_text())["items"][key]["current"]
        self.assertEqual(current["engagement"], "unknown")
        self.assertEqual(current["rate"], "unstated")
        self.assertEqual((current["pay_min"], current["pay_max"], current["pay_currency"],
                          current["pay_period"], current["pay_source"], current["ir35"]),
                         ("", "", "", "", "", ""))

    def test_text_pay_parsing(self):
        self.assertEqual(sb.text_pay("Salary: £70,000 - £85,000 per annum plus bonus"), (70000, 85000, "GBP", "year"))
        self.assertEqual(sb.text_pay("We pay £95k–£110k base salary"), (95000, 110000, "GBP", "year"))
        self.assertEqual(sb.text_pay("€600 - €700 per day"), (600, 700, "EUR", "day"))
        self.assertEqual(sb.text_pay("Series B, raised $40m"), None)
        # Agencies state PAYE before umbrella; the advert's rate is the span of both.
        cpl = "Inside IR35. PAYE rate: £463 per day (inc. holiday pay). Umbrella rate: £537 per day."
        self.assertEqual(sb.text_pay(cpl), (463, 537, "GBP", "day"))
        self.assertEqual(sb.day_rate({"text": cpl}), ("£463–537/day", 537))
        self.assertEqual(sb.day_rate({"text": "£550-£650 per day, Outside IR35"}), ("£550–650/day", 650))
        # A salary is still the first figure; a later day figure in another currency does not join it.
        self.assertEqual(sb.text_pay("Salary £80,000 per annum. Contractors are considered at a separate £600 per day"), (80000, 80000, "GBP", "year"))
        self.assertEqual(sb.text_pay("€600 per day, or £550 per day"), (600, 600, "EUR", "day"))
        self.assertEqual(sb.ir35_of("This role sits Inside IR35"), "inside")
        self.assertEqual(sb.ir35_of("IR35 status to be confirmed"), "")

    def test_dedup_across_runs(self):
        sb.run(self.portals, self.out, "2026-09-14", self.fx)
        status2 = sb.run(self.portals, self.out, "2026-09-15", self.fx)
        self.assertEqual(status2["totals"]["new"], 0)
        second = (self.out / "pending" / "2026-09-15.tsv").read_text().splitlines()
        self.assertEqual(len(second), 1)  # header only

    def test_same_day_rerun_preserves_candidates_and_recovers_seen_write(self):
        sb.run(self.portals, self.out, "2026-09-14", self.fx)
        tsv = self.out / "pending/2026-09-14.tsv"
        original = tsv.read_bytes()
        # A retry must retain already surfaced candidates, even if saving seen.json failed.
        for missing_seen in (False, True):
            with self.subTest(missing_seen=missing_seen):
                if missing_seen:
                    (self.out / "pending/seen.json").unlink()
                status = sb.run(self.portals, self.out, "2026-09-14", self.fx)
                self.assertEqual(tsv.read_bytes(), original)
                self.assertEqual(status["totals"]["new"], 0)
                self.assertEqual(status["totals"]["preferred_new"], 0)
                self.assertEqual(len(json.loads((tsv.parent / "seen.json").read_text())), 5)
        updated = dict(GH, jobs=GH["jobs"] + [dict(GH["jobs"][0], absolute_url="https://gh/new")])
        (self.fx / "greenhouse-ghco.json").write_text(json.dumps(updated))
        with patch.object(sb.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                sb.run(self.portals, self.out, "2026-09-14", self.fx)
        self.assertEqual(tsv.read_bytes(), original)
        status = sb.run(self.portals, self.out, "2026-09-14", self.fx)
        self.assertEqual(len(read_rows(tsv)), 6)
        self.assertEqual(status["totals"]["new"], 1)
        self.assertEqual(status["totals"]["preferred_new"], 1)

    def test_smartrecruiters_pagination_reports_incomplete_results(self):
        first = SMARTRECRUITERS["content"][0]
        second = dict(first, id="7002")
        for total, later, cap, expected in ((2, [second], 10, "ok"), (2, [], 10, "partial"),
                                             (2, [second], 1, "partial"), (2, [first], 10, "partial")):
            with self.subTest(total=total, later=later, cap=cap):
                with patch.object(sb, "fetch_json", side_effect=[dict(SMARTRECRUITERS, totalFound=total), {"content": later}]), \
                     patch.object(sb, "SMARTRECRUITERS_MAX_PAGES", cap):
                    payload = sb.fetch_board("smartrecruiters", "SRCo")
                (self.fx / "smartrecruiters-SRCo.json").write_text(json.dumps(payload))
                status = sb.run(self.portals, self.out, "2026-09-14", self.fx)
                self.assertEqual(status["boards"]["SR Co"]["state"], expected)

    def test_explicit_residence_rules_override_remote_location(self):
        rules = {"accept_any_of": ["remote", "london"],
                 "reject_any_of": ["must be based in the us", "us only", "india"]}
        for location, description, expected in (
            ("Remote", "You must be based in the US to apply.", False),
            ("Remote", "Long introduction. " * 200 + "US only", False),
            ("London", "Collaborate with colleagues in the US and India.", True),
            ("Remote - India", "Global team", False),
        ):
            with self.subTest(location=location, description=description[-80:]):
                self.assertEqual(sb.location_ok(location, description, rules)[0], expected)


if __name__ == "__main__":
    unittest.main()


# -------------------------------------------------------------------------------------------
# Query feeds. Employer ATS boards carry that employer's own headcount; UK contract work is
# brokered and reaches the market through aggregators, so these are the rows the boards cannot
# produce. They must pass exactly the same rules, and must not be double-counted against a board.

FEED_PORTALS = PORTALS + """
feed_limits:
  adzuna: {max_calls: 36, min_interval_seconds: 0}
  reed: {max_calls: 40, min_interval_seconds: 0}
feeds:
  - name: Adzuna UK contract
    provider: adzuna
    enabled: true
    fixture: uk
    params: {country: gb, contract: true, app_id_env: T_ADZ_ID, app_key_env: T_ADZ_KEY}
  - name: Reed UK contract
    provider: reed
    enabled: true
    fixture: uk
    params: {contract: true, api_key_env: T_REED_KEY}
  - name: Disabled feed
    provider: adzuna
    enabled: false
    params: {}
  - name: Nonsense feed
    provider: monster
    enabled: true
    params: {}
"""

ADZUNA = {"count": 2, "results": [
    {"title": "Interim Head of Engineering", "description": "<p>6 month contract, London. £750 per day outside IR35.</p>",
     "created": "2026-09-15T09:00:00Z", "redirect_url": "https://adzuna/1",
     "company": {"display_name": "Northwind Retail"}, "location": {"display_name": "London, UK"},
     "salary_min": 195000.0, "salary_max": 195000.0, "contract_type": "contract",
     "category": {"label": "IT Jobs"}},
    {"title": "Test Manager", "description": "Contract role, Leeds. Day rate negotiable.",
     "created": "2026-09-15T09:00:00Z", "redirect_url": "https://adzuna/2",
     "company": {"display_name": "Granite Bank"}, "location": {"display_name": "Leeds, UK"},
     "salary_min": 140000.0, "salary_max": 160000.0, "contract_type": "contract",
     "category": {"label": "IT Jobs"}},
]}
REED = {"totalResults": 2, "results": [
    {"jobId": 5501, "jobTitle": "Head of Quality Engineering", "employerName": "Halberd Group",
     "locationName": "London", "date": "14/09/2026", "jobUrl": "https://reed/5501",
     "jobDescription": "Contract, 9 months, £700 per day.", "minimumSalary": 700.0,
     "maximumSalary": 700.0, "currency": "GBP", "contractType": "Contract"},
    # Already surfaced by the Ashby board in the same run: one job, one candidate row.
    {"jobId": 5502, "jobTitle": "AI Evaluation Lead", "employerName": "Ashby Co",
     "locationName": "Remote", "date": "12/09/2026", "jobUrl": "https://ab/1",
     "jobDescription": "Contract, outside IR35, £650 per day.", "minimumSalary": 650.0,
     "maximumSalary": 750.0, "currency": "GBP", "contractType": "Contract"},
]}


class FeedTests(unittest.TestCase):
    def setUp(self):
        import collection_schedule
        session=collection_schedule.Session
        self.schedule_patch=patch.object(collection_schedule,'Session',side_effect=lambda data,offline=False:session(data,offline=True))
        self.schedule_patch.start();self.addCleanup(self.schedule_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.portals = root / "portals.yml"
        self.portals.write_text(FEED_PORTALS)
        self.fx = root / "fx"
        self.fx.mkdir()
        (self.fx / "greenhouse-ghco.json").write_text(json.dumps(GH))
        (self.fx / "lever-leverco.json").write_text(json.dumps(LEVER))
        (self.fx / "ashby-ashbyco.json").write_text(json.dumps(ASHBY))
        (self.fx / "smartrecruiters-SRCo.json").write_text(json.dumps(SMARTRECRUITERS))
        (self.fx / "feed-adzuna-uk.json").write_text(json.dumps(ADZUNA))
        (self.fx / "feed-reed-uk.json").write_text(json.dumps(REED))
        self.out = root / "out"
        import yaml
        policy_patch=patch.object(sb,'feed_limits',side_effect=lambda:yaml.safe_load(self.portals.read_text()).get('feed_limits', {'adzuna':{'max_calls':10,'min_interval_seconds':0},'reed':{'max_calls':10,'min_interval_seconds':0}}))
        policy_patch.start();self.addCleanup(policy_patch.stop)


    def tearDown(self):
        self.tmp.cleanup()

    def test_feed_rows_reach_the_tsv_with_the_advertised_company(self):
        status = sb.run(self.portals, self.out, "2026-09-16", self.fx)
        by_url = {r["url"]: r for r in read_rows(self.out / "pending" / "2026-09-16.tsv")}
        self.assertIn("https://adzuna/1", by_url)
        row = by_url["https://adzuna/1"]
        # The company is the advertising employer from the payload, not the feed's own name.
        self.assertEqual(row["company"], "Northwind Retail")
        self.assertEqual(row["engagement"], "interim")  # the title decides, ahead of contract_type
        self.assertEqual(row["provider"], "adzuna")
        self.assertEqual(status["feeds"]["Adzuna UK contract"]["state"], "ok")
        self.assertEqual(status["totals"]["feeds"], 3)  # disabled feeds are not counted

    def test_permanent_description_overrides_feed_contract_with_provenance(self):
        advert = dict(ADZUNA["results"][1], title="Engineering Manager", source_id="permanent",
                      description="Contract: Full Time, Permanent. Lead software engineering.")
        (self.fx / "feed-adzuna-uk.json").write_text(json.dumps({"count": 1, "results": [advert]}))
        sb.run(self.portals, self.out, "2026-09-16", self.fx)
        row = next(r for r in read_rows(self.out / "pending/2026-09-16.tsv") if r["url"] == advert["redirect_url"])
        self.assertEqual(row["engagement"], "permanent")
        self.assertEqual(row["employment_type"], "contract")
        self.assertIn("employment_type", row["why"])
        current = next(v["current"] for v in self.items().values() if v["current"].get("url") == advert["redirect_url"])
        self.assertEqual(current["text"], advert["description"])
        self.assertEqual(current["employment_type"], "contract")
        self.assertEqual(current["qualification"]["engagement"], "permanent")
        self.assertTrue(current["qualification"]["conflicts"])

    def test_facilities_role_is_observed_but_not_surfaced_as_a_candidate(self):
        facilities = dict(ADZUNA["results"][1], title="Engineering Manager",
                          description="Lead HVAC maintenance and building services.")
        software = dict(facilities, redirect_url="https://adzuna/software",
                        description="Lead software engineering for building management systems.")
        (self.fx / "feed-adzuna-uk.json").write_text(json.dumps({"count": 2, "results": [facilities, software]}))
        sb.run(self.portals, self.out, "2026-09-16", self.fx)
        urls = {r["url"] for r in read_rows(self.out / "pending/2026-09-16.tsv")}
        self.assertNotIn(facilities["redirect_url"], urls)
        self.assertIn(software["redirect_url"], urls)
        current = next(v["current"] for v in self.items().values() if v["current"].get("url") == facilities["redirect_url"])
        self.assertFalse(current["qualification"]["eligible"])
        self.assertEqual(current["text"], facilities["description"])

    def test_aggregator_salary_is_never_read_as_a_day_rate(self):
        """Adzuna annualises and Reed states no unit, so a feed figure carries no period and is
        never compared against the day-rate floor. A day rate comes from the advert text only."""
        sb.run(self.portals, self.out, "2026-09-16", self.fx)
        by_url = {r["url"]: r for r in read_rows(self.out / "pending" / "2026-09-16.tsv")}
        # Advert text states the rate: that is what is recorded, not the 195000 the feed sent.
        stated = by_url["https://adzuna/1"]
        self.assertEqual(stated["rate"], "£750/day")
        self.assertEqual((stated["pay_min"], stated["pay_period"], stated["pay_source"]), ("750", "day", "text"))
        # No rate in the text: the feed's figures are kept, with the period explicitly unknown,
        # and the row survives because an unknown period cannot be tested against the floor.
        unstated = by_url["https://adzuna/2"]
        self.assertEqual((unstated["pay_min"], unstated["pay_max"], unstated["pay_currency"]), ("140000", "160000", "GBP"))
        self.assertEqual((unstated["pay_period"], unstated["pay_source"]), ("", "feed"))

    def test_a_job_on_a_board_and_a_feed_is_surfaced_once(self):
        sb.run(self.portals, self.out, "2026-09-16", self.fx)
        rows = read_rows(self.out / "pending" / "2026-09-16.tsv")
        urls = [r["url"] for r in rows]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(urls.count("https://ab/1"), 1)
        # The board read it first, so the board keeps it; the feed reports it as already seen.
        self.assertEqual(next(r for r in rows if r["url"] == "https://ab/1")["provider"], "ashby")

    def test_reed_dates_and_ids(self):
        jobs = sb.normalise_feed("reed", REED)
        self.assertEqual(jobs[0]["posted"], "2026-09-14")
        self.assertEqual(jobs[0]["company"], "Halberd Group")
        # jobUrl absent: fall back to the canonical id-based URL rather than dropping the row.
        fallback = sb.normalise_feed("reed", {"results": [{"jobId": 77, "jobTitle": "Test Manager", "date": "bad-date"}]})
        self.assertEqual(fallback[0]["url"], "https://www.reed.co.uk/jobs/77")
        self.assertEqual(fallback[0]["posted"], "")

    def test_a_missing_credential_isolates_the_feed(self):
        """A feed that cannot authenticate must not cost the run its board rows."""
        boards = lambda provider, slug: json.loads((self.fx / f"{provider}-{slug}.json").read_text())
        with patch.object(sb, "fetch_board", side_effect=boards):
            status = sb.run(self.portals, self.out, "2026-09-16", None)
        self.assertEqual(status["boards"]["GH Co"]["state"], "ok")
        self.assertEqual(status["feeds"]["Adzuna UK contract"]["state"], "config")
        self.assertIn("T_ADZ_ID", status["feeds"]["Adzuna UK contract"]["detail"])
        self.assertEqual(status["feeds"]["Nonsense feed"]["state"], "unsupported-provider")

    def test_engagement_yield_is_reported(self):
        status = sb.run(self.portals, self.out, "2026-09-16", self.fx)
        eng = status["totals"]["by_engagement"]
        self.assertEqual(sum(eng.values()), status["totals"]["new"])
        self.assertGreaterEqual(eng.get("contract", 0), 3)  # adzuna/1, adzuna/2, reed/5501

    def items(self):
        return json.loads((self.out / "pending" / "observations.json").read_text())["items"]

    def test_an_advert_missing_from_a_feed_window_is_not_absent(self):
        """A feed is a moving search window: an advert older than max_days_old drops out while still open."""
        sb.run(self.portals, self.out, "2026-09-16", self.fx)
        (self.fx / "feed-adzuna-uk.json").write_text(json.dumps({"count": 1, "results": ADZUNA["results"][:1]}))
        sb.run(self.portals, self.out, "2026-09-17", self.fx)
        aged = next(v for v in self.items().values() if v["current"].get("url") == "https://adzuna/2")
        self.assertEqual(aged["source_lifecycle"], "active")
        self.assertEqual(aged["absence_observations"], [])

    def test_a_stated_expiry_closes_a_feed_advert(self):
        expiring = dict(REED["results"][0], expirationDate="20/09/2026")
        (self.fx / "feed-reed-uk.json").write_text(json.dumps({"totalResults": 1, "results": [expiring]}))
        sb.run(self.portals, self.out, "2026-09-16", self.fx)
        (self.fx / "feed-reed-uk.json").write_text(json.dumps({"totalResults": 0, "results": []}))
        sb.run(self.portals, self.out, "2026-09-19", self.fx)
        item = next(v for v in self.items().values() if v["source_id"] == "5501")
        self.assertEqual(item["source_lifecycle"], "active")
        sb.run(self.portals, self.out, "2026-09-21", self.fx)
        item = next(v for v in self.items().values() if v["source_id"] == "5501")
        self.assertEqual((item["source_lifecycle"], item["closure_reason"]), ("closed", "source-expiry:2026-09-20"))

    def test_a_run_without_feeds_reads_boards_only_and_keeps_earlier_feed_sightings(self):
        """The free-sources daily run skips the keyed feeds; what the feeds last saw stays as it was."""
        sb.run(self.portals, self.out, "2026-09-16", self.fx)
        before = json.loads((self.out / "pending" / "observations.json").read_text())
        feed_scopes = {s: v for s, v in before["sources"].items() if s.startswith("feed:")}
        self.assertTrue(feed_scopes)
        with patch.object(sb, "fetch_feed", side_effect=AssertionError("no feed request")):
            status = sb.run(self.portals, self.out, "2026-09-17", self.fx, feeds=False)
        self.assertEqual((status["feeds"], status["totals"]["feeds"]), ({}, 0))
        self.assertTrue(status["boards"])
        after = json.loads((self.out / "pending" / "observations.json").read_text())
        self.assertEqual({s: after["sources"][s] for s in feed_scopes}, feed_scopes)
        adzuna = next(v for v in after["items"].values() if v["current"].get("url") == "https://adzuna/1")
        self.assertEqual((adzuna["source_lifecycle"], adzuna["absence_observations"]), ("active", []))

    def test_reed_states_permanent_work_and_expiry(self):
        job = sb.normalise_feed("reed", {"results": [dict(REED["results"][0], contractType="Permanent",
                                                            expirationDate="31/10/2026")]})[0]
        self.assertEqual((job["employment_type"], job["expires"]), ("permanent", "2026-10-31"))

    def test_reed_details_are_read_once_per_advert(self):
        summaries = {"totalResults": 2, "results": [{"jobId": 1, "date": "14/09/2026"}, {"jobId": 2, "date": "14/09/2026"}]}
        calls = []

        def fake(url, headers=None):
            calls.append(url)
            return summaries if "/search" in url else {"jobDescription": "detail " + url[-1]}

        cache = {}
        with patch.object(sb, "fetch_json", side_effect=fake), \
             patch.object(sb, "feed_request", return_value=("https://www.reed.co.uk/api/1.0/search?x", {})):
            first = sb.fetch_feed("reed", {"max_pages": 1}, cache=cache, today="2026-09-16")
            second = sb.fetch_feed("reed", {"max_pages": 1}, cache=cache, today="2026-09-17")
        self.assertEqual((first["_calls"], second["_calls"]), (3, 1))
        self.assertEqual(first["results"], second["results"])

    def test_reed_changed_summary_refreshes_details_even_when_date_is_unchanged(self):
        old = {"jobId": 1, "date": "24/09/2026", "jobTitle": "QA Lead", "minimumSalary": 500}
        changed = dict(old, jobTitle="Head of QA", minimumSalary=800)
        cache = {}
        with patch.object(sb, "feed_request", return_value=("https://reed/search", {})), \
             patch.object(sb, "fetch_json", side_effect=[
                 {"totalResults": 1, "results": [old]}, dict(old, contractType="Contract"),
                 {"totalResults": 1, "results": [changed]}, dict(changed, contractType="Permanent"),
             ]):
            sb.fetch_feed("reed", {"max_pages": 1}, cache=cache, today="2026-09-24")
            refreshed = sb.fetch_feed("reed", {"max_pages": 1}, cache=cache, today="2026-09-25")
        self.assertEqual(refreshed["results"][0]["jobTitle"], "Head of QA")
        self.assertEqual(refreshed["results"][0]["minimumSalary"], 800)
        self.assertEqual(refreshed["results"][0]["contractType"], "Permanent")
        self.assertEqual(refreshed["_calls"], 2)

    def test_reed_changed_summary_does_not_reuse_old_details_when_budget_is_spent(self):
        old = {"jobId": 1, "date": "24/09/2026", "jobTitle": "QA Lead"}
        cache = {}
        with patch.object(sb, "feed_request", return_value=("https://reed/search", {})), \
             patch.object(sb, "fetch_json", side_effect=[
                 {"totalResults": 1, "results": [old]}, dict(old, contractType="Contract"),
                 {"totalResults": 1, "results": [dict(old, jobTitle="Head of QA")]},
             ]):
            sb.fetch_feed("reed", {"max_pages": 1}, cache=cache)
            refreshed = sb.fetch_feed("reed", {"max_pages": 1}, cache=cache, budget={"left": 0})
        self.assertEqual(refreshed["results"][0]["jobTitle"], "Head of QA")
        self.assertNotIn("contractType", refreshed["results"][0])

    def test_reed_application_count_does_not_cost_another_detail_read(self):
        summary = {"jobId": 1, "date": "24/09/2026", "jobTitle": "QA Lead", "applications": 10}
        with patch.object(sb, "feed_request", return_value=("https://reed/search", {})), \
             patch.object(sb, "fetch_json", side_effect=[
                 {"totalResults": 1, "results": [summary]}, {"contractType": "Contract"},
                 {"totalResults": 1, "results": [dict(summary, applications=11)]},
             ]):
            cache = {}
            sb.fetch_feed("reed", {"max_pages": 1}, cache=cache)
            refreshed = sb.fetch_feed("reed", {"max_pages": 1}, cache=cache)
        self.assertEqual(refreshed["_calls"], 1)
        self.assertEqual(refreshed["results"][0]["applications"], 11)

    def test_reed_details_are_read_only_for_adverts_the_rules_keep_and_within_a_budget(self):
        summaries = {"totalResults": 3, "results": [{"jobId": 1, "jobTitle": "Sales Director", "date": "d"},
                                                    {"jobId": 2, "jobTitle": "Head of QA", "date": "d"},
                                                    {"jobId": 3, "jobTitle": "Head of QA II", "date": "d"}]}
        calls = []

        def fake(url, headers=None):
            calls.append(url)
            return summaries if "/search" in url else {"jobDescription": "detail"}

        budget = {"left": 1}
        with patch.object(sb, "fetch_json", side_effect=fake), \
             patch.object(sb, "feed_request", return_value=("https://www.reed.co.uk/api/1.0/search?x", {})):
            out = sb.fetch_feed("reed", {"max_pages": 1}, cache={}, today="2026-09-16",
                                wants_detail=lambda s: "Sales" not in s.get("jobTitle", ""), budget=budget)
        self.assertEqual([u for u in calls if "/jobs/" in u], ["https://www.reed.co.uk/api/1.0/jobs/2"])
        self.assertEqual((out["_calls"], budget["left"]), (2, 0))

    def test_a_live_run_keeps_the_reed_detail_cache(self):
        def fake(url, headers=None):
            if "adzuna" in url:
                return ADZUNA
            return REED if "/search" in url else {"expirationDate": "31/10/2026"}
        keys = {"T_ADZ_ID": "i", "T_ADZ_KEY": "k", "T_REED_KEY": "r"}
        with patch.dict(os.environ, keys), patch.object(sb, "fetch_json", side_effect=fake), \
             patch.object(sb, "fetch_board", side_effect=RuntimeError("offline")):
            status = sb.run(self.portals, self.out, "2026-09-16", None)
        cache = json.loads((self.out / "pending" / "feeds" / "reed-details.json").read_text())
        self.assertEqual(sorted(cache), ["5501", "5502"])
        self.assertEqual(status["feeds"]["Reed UK contract"]["calls"], 3)

    def test_reed_detail_429_stops_that_provider_and_reports_failed_call(self):
        import yaml
        cfg = yaml.safe_load(FEED_PORTALS)
        cfg["tracked_companies"] = []
        reed = next(f for f in cfg["feeds"] if f["provider"] == "reed")
        cfg["feeds"] = [reed, dict(reed, name="Reed second search")]
        self.portals.write_text(yaml.safe_dump(cfg))
        calls = []

        def fake(url, headers=None):
            calls.append(url)
            if "/search" in url:
                return REED
            raise urllib.error.HTTPError(url, 429, "Too many requests", {"Retry-After": "60"}, None)

        with patch.dict(os.environ, {"T_REED_KEY": "fixture"}), patch.object(sb, "fetch_json", side_effect=fake):
            status = sb.run(self.portals, self.out, "2026-09-24")
        self.assertEqual(len(calls), 2)  # one search and one denied detail; no more requests
        self.assertEqual(status["feeds"]["Reed UK contract"]["state"], "http-429")
        self.assertEqual(status["feeds"]["Reed UK contract"]["calls"], 2)
        self.assertEqual(status["feeds"]["Reed second search"]["calls"], 0)

    def test_provider_call_budget_is_shared_between_feeds(self):
        import yaml
        cfg = yaml.safe_load(FEED_PORTALS)
        cfg["tracked_companies"] = []
        adzuna = next(f for f in cfg["feeds"] if f["provider"] == "adzuna")
        cfg["feeds"] = [adzuna, dict(adzuna, name="Adzuna second search")]
        cfg["feed_limits"]["adzuna"]["max_calls"] = 1
        self.portals.write_text(yaml.safe_dump(cfg))
        calls = []

        def fake(url, headers=None):
            calls.append(url)
            return ADZUNA

        with patch.dict(os.environ, {"T_ADZ_ID": "i", "T_ADZ_KEY": "k"}), patch.object(sb, "fetch_json", side_effect=fake):
            status = sb.run(self.portals, self.out, "2026-09-24")
        self.assertEqual(len(calls), 1)
        self.assertEqual(status["feeds"]["Adzuna second search"]["state"], "budget")
        self.assertEqual(status["feeds"]["Adzuna second search"]["calls"], 0)

    def test_call_cap_keeps_already_read_pages_as_incomplete_evidence(self):
        with patch.object(sb, 'feed_request', return_value=('https://example.org/search', {})):
            payload = sb.fetch_feed('adzuna', {'max_pages': 3}, request=unittest.mock.Mock(side_effect=[
                {'results': [{'id': 'one'}], 'count': 3}, sb.FeedStopped('budget')]))
        self.assertEqual(payload['results'], [{'id': 'one'}])
        self.assertEqual((payload['_pages'], payload['_calls'], payload['_complete'], payload['_stop_reason']),
                         (1, 1, False, 'budget'))

    def test_call_cap_during_reed_details_keeps_current_page_summaries(self):
        summaries = {'results': [{'jobId': 1}, {'jobId': 2}], 'totalResults': 2}
        with patch.object(sb, 'feed_request', return_value=('https://example.org/search', {})):
            payload = sb.fetch_feed('reed', {'max_pages': 1}, request=unittest.mock.Mock(side_effect=[
                summaries, {'jobDescription': 'Read detail'}, sb.FeedStopped('budget')]))
        self.assertEqual(payload['results'], [{'jobId': 1, 'jobDescription': 'Read detail'}, {'jobId': 2}])
        self.assertEqual((payload['_calls'], payload['_complete'], payload['_stop_reason']), (2, False, 'budget'))

    def test_malformed_reed_detail_counts_once_and_is_not_cached(self):
        cache = {}
        request = unittest.mock.Mock(side_effect=[{'results': [{'jobId': 1}], 'totalResults': 1}, []])
        with patch.object(sb, 'feed_request', return_value=('https://example.org/search', {})):
            payload = sb.fetch_feed('reed', {'max_pages': 1}, cache=cache, request=request)
        self.assertEqual((request.call_count, payload['_calls']), (2, 2))
        self.assertEqual(payload['results'], [{'jobId': 1}])
        self.assertEqual(cache, {})

    def test_production_searches_have_no_routine_second_pages(self):
        import yaml
        cfg=yaml.safe_load((Path(__file__).resolve().parents[1]/'config/portals.yml').read_text())
        self.assertEqual([f['params']['max_pages'] for f in cfg['feeds']],[1,1,1])
        self.assertEqual(sum(len(f['params']['phrases']) for f in cfg['feeds'] if f['provider']=='adzuna'),21)

    def test_quota_counted_requests_are_never_retried(self):
        """Every attempt spends Adzuna or Reed quota, so the budget and the probe read once per call."""
        import web_read
        attempts = []

        def fake(url, headers=None):
            attempts.append(web_read.POLICY.disabled)
            return ADZUNA

        with patch.dict(os.environ, {"T_ADZ_ID": "i", "T_ADZ_KEY": "k"}), patch.object(sb, "fetch_json", side_effect=fake):
            sb.FeedRequests({"adzuna": {"max_calls": 2, "min_interval_seconds": 0}}).read(
                "adzuna", "https://api.adzuna.com/x")
            with patch.object(sb, "feed_request", return_value=("https://x", {})):
                sb.probe_feed("adzuna", {})
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(attempts), "each quota-counted call runs inside single_attempt()")

    def test_production_adzuna_calls_fit_the_revised_trial_limits(self):
        """Pin independent limits observed in the account on 29 Oct, not config-derived targets."""
        import yaml
        cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config/portals.yml").read_text())
        clock, starts = [0.0], []

        def read(url, headers=None):
            starts.append(clock[0])
            return ADZUNA

        def sleep(seconds):
            clock[0] += seconds

        requests = sb.FeedRequests({'adzuna': {'max_calls':10,'min_interval_seconds':4.1}})
        with patch("time.monotonic", side_effect=lambda: clock[0]), patch("time.sleep", side_effect=sleep), \
             patch.object(sb, "fetch_json", side_effect=read):
            for _ in range(10):
                requests.read("adzuna", "https://api.adzuna.com/fixture")
            with self.assertRaises(sb.FeedStopped):
                requests.read("adzuna", "https://api.adzuna.com/fixture")
        self.assertEqual(len(starts), 10)
        self.assertLessEqual(max(sum(0 <= at - earlier < 60 for earlier in starts) for at in starts), 15)
        # Ten requests/day: at most 310 in a 31-day month.
        # This is not an account-wide or cross-run quota assertion.
        self.assertLessEqual(len(starts) * 31, 1500)

    def test_missing_provider_policy_fails_closed_without_network(self):
        with patch.object(sb, "fetch_json") as fetch:
            with self.assertRaises(sb.FeedConfigError):
                sb.FeedRequests({}).read("adzuna", "https://api.adzuna.com/fixture")
        fetch.assert_not_called()

    def test_cli_probe_obeys_zero_provider_budget(self):
        import yaml
        cfg = yaml.safe_load(FEED_PORTALS)
        cfg["feed_limits"]["adzuna"]["max_calls"] = 0
        self.portals.write_text(yaml.safe_dump(cfg))
        with patch.object(sb, "feed_request", return_value=("https://api.adzuna.com/fixture", {})), \
             patch.object(sb, "fetch_json", return_value=ADZUNA) as fetch:
            self.assertEqual(sb.main(["--portals", str(self.portals), "--probe", "adzuna"]), 1)
        fetch.assert_not_called()

    def test_provider_requests_are_spaced_across_feeds(self):
        import yaml
        cfg = yaml.safe_load(FEED_PORTALS)
        cfg["tracked_companies"] = []
        feed = next(f for f in cfg["feeds"] if f["provider"] == "adzuna")
        cfg["feeds"] = [feed, dict(feed, name="Second search")]
        cfg["feed_limits"]["adzuna"]["min_interval_seconds"] = 2.6
        self.portals.write_text(yaml.safe_dump(cfg))
        clock, starts = [0.0], []

        def fake(url, headers=None):
            starts.append(clock[0])
            return ADZUNA

        def sleep(seconds):
            clock[0] += seconds

        with patch.dict(os.environ, {"T_ADZ_ID": "i", "T_ADZ_KEY": "k"}), \
             patch("time.monotonic", side_effect=lambda: clock[0]), patch("time.sleep", side_effect=sleep), \
             patch.object(sb, "fetch_json", side_effect=fake):
            sb.run(self.portals, self.out, "2026-09-24")
        self.assertEqual(starts, [0.0, 2.6])

    def test_phrase_searches_merge_into_one_feed(self):
        """Each role phrase is its own exact search; the feed is their union, read once per advert."""
        pages = {"head of quality": [{"count": 2, "results": [{"id": 1, "title": "Head of Quality Assurance"},
                                                              {"id": 2, "title": "Head of QA"}]}],
                 "QA lead": [{"count": 2, "results": [{"id": 2, "title": "Head of QA"},
                                                      {"id": 3, "title": "QA Engineering Lead"}]}]}
        seen = []

        def fake_request(provider, params, page, phrase=None):
            seen.append((phrase, page))
            return (f"https://x/{phrase}/{page}", {})

        with patch.object(sb, "feed_request", side_effect=fake_request), \
             patch.object(sb, "fetch_json", side_effect=lambda url, headers=None: pages[url.split("/")[3]].pop(0)):
            out = sb.fetch_feed("adzuna", {"phrases": ["head of quality", "QA lead"], "max_pages": 2})
        self.assertEqual([r["id"] for r in out["results"]], [1, 2, 3])
        self.assertEqual((out["_calls"], out["_complete"]), (2, True))
        self.assertEqual(seen, [("head of quality", 1), ("QA lead", 1)])

    def test_feed_request_building(self):
        os.environ["T_ADZ_ID"], os.environ["T_ADZ_KEY"] = "id1", "key1"
        try:
            url, headers = sb.feed_request("adzuna", {"country": "gb", "contract": True,
                                                      "app_id_env": "T_ADZ_ID", "app_key_env": "T_ADZ_KEY",
                                                      "what_or": "test manager"}, 2)
            phrase, _ = sb.feed_request("adzuna", {"permanent": True, "app_id_env": "T_ADZ_ID",
                                                   "app_key_env": "T_ADZ_KEY"}, 1, phrase="head of quality")
        finally:
            del os.environ["T_ADZ_ID"], os.environ["T_ADZ_KEY"]
        self.assertIn("/jobs/gb/search/2?", url)
        self.assertIn("contract=1", url)
        self.assertNotIn("contract_type", url)
        self.assertIn("what_phrase=head+of+quality", phrase)
        with patch.dict(os.environ, {"T_ADZ_ID": "i", "T_ADZ_KEY": "k"}):
            remote, _ = sb.feed_request("adzuna", {"what": "remote", "sort_by": "date", "app_id_env": "T_ADZ_ID",
                                                   "app_key_env": "T_ADZ_KEY"}, 1)
        self.assertIn("what=remote", remote)
        self.assertIn("sort_by=date", remote)
        self.assertIn("permanent=1", phrase)
        self.assertIn("what_or=test+manager", url)
        self.assertEqual(headers, {})
        os.environ["T_REED_KEY"] = "rk"
        try:
            url, headers = sb.feed_request("reed", {"api_key_env": "T_REED_KEY", "contract": True,
                                                    "results_per_page": 50, "where": "London"}, 3)
        finally:
            del os.environ["T_REED_KEY"]
        self.assertIn("resultsToSkip=100", url)
        self.assertIn("locationName=London", url)
        self.assertIn("contract=true", url)
        self.assertEqual(headers["Authorization"], "Basic " + base64.b64encode(b"rk:").decode())
        os.environ["T_REED_KEY"] = "rk"
        try:
            url, _ = sb.feed_request("reed", {"api_key_env": "T_REED_KEY"}, 1, phrase="QA lead")
        finally:
            del os.environ["T_REED_KEY"]
        self.assertIn("keywords=QA+lead", url)
        with self.assertRaises(sb.FeedConfigError):
            sb.feed_request("adzuna", {"app_id_env": "T_ABSENT_ID"}, 1)

    def test_probe_names_the_fields_that_moved(self):
        """The adapters were written from published schemas, not from a live call, so the probe is
        how a feed earns being enabled."""
        moved = {"results": [{"title": "X", "description": "d", "created": "2026-09-15T00:00:00Z",
                              "redirect_url": "u", "company": {"display_name": "C"},
                              "location": {"display_name": "London"}, "employment_type": "contract",
                              "category": {"label": "IT"}}]}
        with patch.object(sb, "fetch_json", return_value=moved), \
             patch.object(sb, "feed_request", return_value=("https://x", {})):
            r = sb.probe_feed("adzuna", {})
        self.assertTrue(r["ok"])
        self.assertIn("contract_type", r["optional_missing"])
        self.assertIn("salary_min", r["optional_missing"])
        self.assertIn("employment_type", r["unexpected_item_keys"])
        self.assertEqual(r["present"]["company.display_name"], 1)

    def test_truncated_paging_is_reported_partial(self):
        pages = [{"results": [{"title": f"Test Manager {i}", "redirect_url": f"https://a/{i}",
                               "description": "", "created": "", "company": {}, "location": {}}], "count": 99}
                 for i in range(3)]
        with patch.object(sb, "fetch_json", side_effect=pages), \
             patch.object(sb, "feed_request", return_value=("https://x", {})):
            payload = sb.fetch_feed("adzuna", {"max_pages": 3})
        self.assertFalse(payload["_complete"])  # 3 read of 99 available
        self.assertEqual(payload["_pages"], 3)


class ConfiguredTitleTests(unittest.TestCase):
    """The live title filter against adverts the owner found that the scan should have surfaced."""

    def setUp(self):
        import yaml
        cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config/portals.yml").read_text())
        self.tf = cfg["title_filter"]

    def test_quality_leadership_adverts_match(self):
        for title in ("Test Architect - Non Functional & Performance",
                      "Non-Functional & Performance Test Architect",
                      "Senior QA Lead (Contract: Outside IR35) – FINTECH / LONDON",
                      "Quality Engineering Lead / Test Lead",
                      "Head of Testing", "QA Architect"):
            self.assertTrue(sb.title_match(title, self.tf)[0], title)

    def test_infrastructure_reliability_stays_out(self):
        # SRE asks for production Kubernetes and Terraform; it is not a quality-leadership title.
        self.assertFalse(sb.title_match("Site Reliability Engineer", self.tf)[0])


class RetentionTests(unittest.TestCase):
    """Adverts whose titles cannot match keep identity and short facts, unless a record needs them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.portals = root / "portals.yml"
        self.portals.write_text(FEED_PORTALS)
        self.fx = root / "fx"
        self.fx.mkdir()
        board = json.loads(json.dumps(GH))
        board["jobs"].append({"title": "Sales Director", "location": {"name": "London"}, "absolute_url": "https://gh/20",
                              "content": "<p>6 month contract, £800 per day outside IR35</p>"})
        (self.fx / "greenhouse-ghco.json").write_text(json.dumps(board))
        (self.fx / "lever-leverco.json").write_text(json.dumps(LEVER))
        (self.fx / "ashby-ashbyco.json").write_text(json.dumps(ASHBY))
        (self.fx / "smartrecruiters-SRCo.json").write_text(json.dumps(SMARTRECRUITERS))
        feed = json.loads(json.dumps(ADZUNA))
        feed["results"].append(dict(feed["results"][0], title="Sales Director", redirect_url="https://adzuna/9"))
        (self.fx / "feed-adzuna-uk.json").write_text(json.dumps(feed))
        (self.fx / "feed-reed-uk.json").write_text(json.dumps(REED))
        self.out = root / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def protect(self, identities=(), urls=(), roles=()):
        from pending_store import PROTECTION, write_protection
        write_protection(self.out / "pending" / PROTECTION, at="2026-09-16T05:30:00Z",
                         identities=identities, urls=urls, roles=roles)

    def item(self, url):
        items = json.loads((self.out / "pending" / "observations.json").read_text())["items"]
        return next(v for v in items.values() if v["current"].get("url") == url)

    def test_without_a_manifest_every_advert_keeps_its_text(self):
        status = sb.run(self.portals, self.out, "2026-09-16", self.fx)
        for url in ("https://gh/6", "https://gh/20", "https://adzuna/9"):
            self.assertIn("text", self.item(url)["current"], url)
            self.assertNotIn("compacted_at", self.item(url))
        self.assertEqual(status["retention"], {"manifest": "missing", "text_dropped": 0, "held": 0})

    def test_unmatched_titles_keep_short_facts_and_stated_terms_without_text(self):
        whole = sb.run(self.portals, self.out, "2026-09-16", self.fx)
        candidates = (self.out / "pending" / "2026-09-16.tsv").read_text()
        shutil.rmtree(self.out)
        self.protect()
        status = sb.run(self.portals, self.out, "2026-09-16", self.fx)
        self.assertEqual((self.out / "pending" / "2026-09-16.tsv").read_text(), candidates)
        self.assertEqual(status["totals"], whole["totals"])
        sales = self.item("https://gh/20")
        self.assertEqual(sales["compaction_reason"], "title-unmatched")
        self.assertFalse(set(sales["current"]) & {"text", "summary", "description", "qualification"})
        self.assertEqual((sales["current"]["title"], sales["current"]["company"], sales["current"]["location"]),
                         ("Sales Director", "GH Co", "London"))
        # Terms are read from the advert before its text goes.
        self.assertEqual((sales["current"]["pay_min"], sales["current"]["pay_period"], sales["current"]["ir35"],
                          sales["current"]["engagement"]), ("800", "day", "outside", "contract"))
        self.assertEqual(self.item("https://adzuna/9")["compaction_reason"], "title-unmatched")
        # Matched titles, kept or rejected later for location or pay, keep everything.
        for url in ("https://gh/1", "https://gh/3", "https://gh/4", "https://adzuna/1"):
            self.assertIn("text", self.item(url)["current"], url)
            self.assertIn("qualification", self.item(url)["current"], url)
            self.assertNotIn("compacted_at", self.item(url), url)
        unmatched = [u for u in ("https://gh/2", "https://gh/6", "https://gh/20", "https://adzuna/9")]
        self.assertTrue(all(self.item(u).get("compaction_reason") == "title-unmatched" for u in unmatched))
        self.assertEqual(status["retention"], {"manifest": "ok", "text_dropped": 4, "held": 0})

    def test_adverts_a_record_holds_keep_their_text(self):
        self.protect(identities=[("board:greenhouse:ghco", "https://gh/2")], urls=["https://gh/6"],
                     roles=[("gh co", "sales director")])
        status = sb.run(self.portals, self.out, "2026-09-16", self.fx)
        for url in ("https://gh/2", "https://gh/6", "https://gh/20"):
            self.assertIn("text", self.item(url)["current"], url)
            self.assertNotIn("compacted_at", self.item(url), url)
        self.assertEqual(self.item("https://adzuna/9")["compaction_reason"], "title-unmatched")
        self.assertEqual(status["retention"], {"manifest": "ok", "text_dropped": 1, "held": 3})

    def test_an_unreadable_manifest_keeps_every_advert_whole(self):
        from pending_store import PROTECTION
        (self.out / "pending").mkdir(parents=True)
        (self.out / "pending" / PROTECTION).write_text('{"schema_version": 1')
        status = sb.run(self.portals, self.out, "2026-09-16", self.fx)
        self.assertIn("text", self.item("https://gh/20")["current"])
        self.assertEqual(status["retention"], {"manifest": "invalid", "text_dropped": 0, "held": 0})

    def test_a_retitled_advert_that_now_matches_regains_its_text_and_becomes_a_candidate(self):
        self.protect()
        sb.run(self.portals, self.out, "2026-09-16", self.fx)
        self.assertEqual(self.item("https://gh/20")["compaction_reason"], "title-unmatched")
        board = json.loads((self.fx / "greenhouse-ghco.json").read_text())
        board["jobs"][-1]["title"] = "Interim Head of Engineering"
        (self.fx / "greenhouse-ghco.json").write_text(json.dumps(board))
        sb.run(self.portals, self.out, "2026-09-17", self.fx)
        item = self.item("https://gh/20")
        self.assertNotIn("compacted_at", item)
        self.assertIn("£800 per day", item["current"]["text"])
        self.assertIn("https://gh/20", (self.out / "pending" / "2026-09-17.tsv").read_text())
