"""Job-alert emails: synthetic fixtures only; no real mailbox or email content is used in tests."""
from email.message import EmailMessage
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_alerts as sa  # noqa: E402

PORTALS = """
title_filter:
  positive: ["Head of Quality", "Engineering Lead", "QA Lead"]
  negative: ["Sales"]
location_filter:
  accept_any_of: ["london", "remote", "united kingdom"]
  reject_any_of: ["singapore"]
engagement_filter: {}
alerts:
  enabled: true
  senders:
    linkedin: [jobalerts-noreply@linkedin.com]
"""

HTML = """<html><body><table>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc"><img alt="Formula."></a></td>
<td><a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc">Head of Quality Assurance</a>
<p>Formula. &middot; London Area, United Kingdom (Hybrid)</p><p>Actively recruiting</p>
<a href="https://www.linkedin.com/comm/jobs/view/4012345678/?x=1">View job</a></td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4099999999/">QA Engineering Lead</a>
<p>Meta &middot; London</p><p>2 connections work here</p></td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4011111111/">Sales Director</a><p>Acme &middot; Singapore</p></td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/search/?keywords=qa">See all jobs</a></td></tr>
</table></body></html>"""


def alert_email(html=HTML, sender="LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", date="Wed, 23 Sep 2026 07:00:00 +0100"):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Date"] = sender, "alerts@example.test", "Head of Quality: 3 new jobs", date
    msg.set_content("plain text part")
    msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


def repeated_cards_html():
    """Synthetic structure matching the card boundary in three retained original alerts."""
    logo = 'https://media.licdn.com/dms/image/v2/ID/company-logo_100_100/0/1'
    def card(jid, image):
        url = f'https://www.linkedin.com/jobs/view/{jid}/'
        return f'''<tr><td class="pt-3"><table><tbody><tr><td>
        <table><tbody><tr><td class="pr-1 w-6"><a href="{url}">
        <img {'src="' + logo + '"' if image else 'alt="Example"'}></a></td>
        <td><a href="{url}"><table><tbody><tr><td class="pb-0">
        <a href="{url}">QA Lead</a></td></tr><tr><td class="pt-3">Example · London</td></tr>
        </tbody></table></a></td></tr></tbody></table></td></tr></tbody></table></td></tr>'''
    return '<table class="leading-regular"><tbody>' + card('901', True) + card('901', False) + card('902', True) + '</tbody></table>'


class ParserTests(unittest.TestCase):
    def test_supported_card_boundaries_count_repeated_jobs_but_not_nested_anchors(self):
        jobs = sa.parse_linkedin(repeated_cards_html())
        self.assertEqual([job['source_id'] for job in jobs], ['901', '901', '902'])
        self.assertEqual([bool(job.get('company_logo_url')) for job in jobs], [True, False, True])
        self.assertEqual([job['company'] for job in jobs], ['Example'] * 3)

    def test_company_logos_belong_only_to_their_own_job_anchor(self):
        first = 'https://media.licdn.com/dms/image/v2/ONE/company-logo_100_100/0/1?e=1794000000&v=beta&t=signed'
        second = first.replace('/ONE/', '/TWO/')
        html = f'''<img src="{first}"><a href="https://www.linkedin.com/jobs/view/901/">
        <img src="https://media.licdn.com/dms/image/v2/BAD/badge/0/1"><img src="{first}"></a>
        <a href="https://www.linkedin.com/jobs/view/901/">Engineering Lead</a><p>First Ltd · London</p>
        <img src="{second}"><a href="https://www.linkedin.com/jobs/view/902/"><img src="{second}"></a>
        <a href="https://www.linkedin.com/jobs/view/902/">QA Lead</a><p>Second Ltd · London</p>
        <a href="https://www.linkedin.com/jobs/view/903/">QA Lead</a><p>Third Ltd · London</p>
        <a href="https://www.linkedin.com/feed/"><img src="{first}"></a>'''
        jobs = sa.parse_linkedin(html)
        self.assertEqual([j.get('company_logo_url') for j in jobs], [first, second, None])
        self.assertEqual([j['company'] for j in jobs], ['First Ltd', 'Second Ltd', 'Third Ltd'])

    def test_invalid_or_ambiguous_logo_urls_are_not_source_facts(self):
        valid = 'https://media.licdn.com/dms/image/v2/ID/company-logo_100_100/0/1?e=1794000000'
        for url in (valid.replace('https:', 'http:'), valid.replace('media.licdn.com', 'media.licdn.com.evil.test'),
                    valid.replace('media.licdn.com', 'user@media.licdn.com'),
                    valid.replace('media.licdn.com', 'media.licdn.com:8443'),
                    valid.replace('/dms/image/', '/other/'), valid.replace('company-logo', 'badge'),
                    'data:image/png;base64,abc', 'cid:company-logo', valid + '\n'):
            with self.subTest(url=url):
                html = f'<a href="https://www.linkedin.com/jobs/view/901/"><img src="{url}">QA Lead</a><p>First · London</p>'
                self.assertNotIn('company_logo_url', sa.parse_linkedin(html)[0])

    def test_numeric_expiry_query_alone_stays_unknown_and_cards_precede_job_deduplication(self):
        import datetime as dt
        from source_images import logo_counts, logo_expiry
        now = dt.datetime(2026, 10, 28, tzinfo=dt.timezone.utc)
        base = 'https://media.licdn.com/dms/image/v2/ID/company-logo_100_100/0/1'
        def url(days):
            return base + '?e=' + str(int((now + dt.timedelta(days=days)).timestamp()))
        jobs = [dict(source_id='same', company_logo_url=url(-1)),
                dict(source_id='same', company_logo_url=url(7)),
                dict(source_id='same', company_logo_url=url(8)),
                dict(company_logo_url=base + '?e=unknown'), dict(company_logo_url=base + '?e=1&e=2'), {}]
        self.assertIsNone(logo_expiry(url(-1)), 'Numeric shape does not validate the meaning of LinkedIn e')
        self.assertEqual(logo_counts(jobs, now), dict(cards=6, logo_cards=5, logo_expired=0,
                         logo_expiring_soon=0, logo_expiry_unknown=5))

    def test_company_logo_fact_survives_pending_text_compaction(self):
        logo = 'https://media.licdn.com/dms/image/v2/ID/company-logo_100_100/0/1?t=signed'
        with tempfile.TemporaryDirectory() as folder:
            store = sa.ObservationStore(Path(folder) / 'observations.json')
            store.observe(scope='alert:linkedin', source_type='job', provider='linkedin_alert',
                observed_at='2026-09-01T09:00:00Z', state='ok', complete=True, absence_authoritative=False,
                items=[dict(source_id='901', current=dict(company='Example', title='QA Lead',
                            company_logo_url=logo, text='Retained source wording.'))])
            self.assertEqual(store.compact('2026-10-28T09:00:00Z', held=lambda item: True), 1)
            item = store.load()['items']['alert:linkedin\x1f901']
            self.assertEqual(item['current']['company_logo_url'], logo)
            self.assertNotIn('text', item['current'])
            self.assertEqual(item['last_seen_at'], '2026-09-01T09:00:00Z')

    def test_salary_is_scoped_to_its_card_and_survives_canonical_import(self):
        html = '''<a href="https://www.linkedin.com/jobs/view/900101/">Head of Engineering</a>
        <p>Example One · London</p><p>£120K–£160K / year salary</p>
        <a href="https://www.linkedin.com/jobs/view/900102/">Engineering Manager</a>
        <p>Example Two · London</p><p>£650–£750 / day</p>
        <a href="https://www.linkedin.com/jobs/view/900103/">QA Lead</a><p>Example Three · London</p>'''
        jobs = sa.parse_linkedin(html)
        import yaml
        sa._eligibility(jobs, yaml.safe_load(PORTALS))
        self.assertEqual([(j.get('pay_min'), j.get('pay_max'), j.get('pay_period')) for j in jobs],
                         [('120000', '160000', 'year'), ('650', '750', 'day'), ('', '', '')])
        self.assertEqual(jobs[0]['engagement'], 'unknown', 'Salary is not proof of permanent employment')
        from tools.test_wo import wodb, wo
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            observations = sa.ObservationStore(folder / 'observations.json')
            observations.observe(scope='alert:linkedin', source_type='job', provider='linkedin_alert',
                observed_at=wodb.now(), state='ok', complete=True, absence_authoritative=False,
                items=[dict(source_id=j['source_id'], current=dict(j, eligible=True)) for j in jobs])
            conn = wodb.connect(folder / 'test.db')
            try:
                wodb.migrate(conn); wo.cmd_import_scans(conn, folder)
                rows = conn.execute("SELECT pay_min,pay_max,pay_period,summary FROM records WHERE type='job' ORDER BY url").fetchall()
                self.assertEqual([tuple(r) for r in rows], [('120000','160000','year',''),('650','750','day',''),('','','','')],
                                 'Pay text must not become evidence of the role remit')
            finally:
                conn.close()

    def test_target_title_variants_pass_intake_and_ranking_for_qualification(self):
        import yaml, datetime as dt, relevance
        whole = yaml.safe_load((sa.ROOT / 'config/portals.yml').read_text())
        for title in ('Head of AI', 'AI Assurance Lead', 'Founding Software Engineer', 'Director of Software Engineering'):
            with self.subTest(title=title):
                jobs = [dict(title=title, location='London')]
                sa._eligibility(jobs, whole)
                self.assertTrue(jobs[0]['eligible'])
                record = dict(id='J', type='job', name=title, location='London')
                score = relevance.score(dict(records=[record], links=[], interactions=[], drafts=[]),
                                        relevance.load_config(), dt.date(2026,10,28))['J']
                self.assertIn(score['band'], ('possible', 'strong'))

    def test_each_job_card_becomes_one_job(self):
        jobs = sa.parse_linkedin(HTML)
        self.assertEqual([j["source_id"] for j in jobs], ["4012345678", "4099999999", "4011111111"])
        first = jobs[0]
        self.assertEqual((first["title"], first["company"], first["location"]),
                         ("Head of Quality Assurance", "Formula.", "London Area, United Kingdom (Hybrid)"))
        self.assertEqual(first["url"], "https://www.linkedin.com/jobs/view/4012345678/")
        self.assertEqual((jobs[1]["company"], jobs[1]["location"]), ("Meta", "London"))

    def test_an_email_without_job_links_yields_nothing(self):
        self.assertEqual(sa.parse_linkedin("<p>Your weekly summary</p>"), [])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.portals = root / "portals.yml"
        self.portals.write_text(PORTALS)
        self.inbox = root / "inbox"
        self.inbox.mkdir()
        self.out = root / "out"

    def items(self):
        return json.loads((self.out / "pending" / "observations.json").read_text())["items"]

    def test_duplicate_card_receipt_counts_precede_same_email_job_deduplication(self):
        (self.inbox / 'repeated.eml').write_bytes(alert_email(repeated_cards_html()))
        status = sa.run(self.portals, self.out, '2026-09-23', self.inbox)
        self.assertEqual((status['cards'], status['logo_cards'], status['jobs']), (3, 2, 2))
        self.assertEqual(len(self.items()), 2)

    def test_logo_counts_precede_job_deduplication_and_fact_survives_import(self):
        logo = 'https://media.licdn.com/dms/image/v2/ID/company-logo_100_100/0/1?t=signed'
        html = HTML.replace('<img alt="Formula.">', f'<img src="{logo}" alt="Formula.">')
        for name in ('one.eml', 'two.eml'):
            (self.inbox / name).write_bytes(alert_email(html))
        status = sa.run(self.portals, self.out, '2026-09-23', self.inbox)
        self.assertEqual({k: status[k] for k in ('cards', 'logo_cards', 'logo_expired',
                         'logo_expiring_soon', 'logo_expiry_unknown')},
                         dict(cards=6, logo_cards=2, logo_expired=0, logo_expiring_soon=0, logo_expiry_unknown=2))
        self.assertEqual(status['jobs'], 3)
        self.assertNotIn(logo, json.dumps(status))
        from tools.test_wo import wodb, wo
        conn = wodb.connect(Path(self.tmp.name) / 'logos.db')
        self.addCleanup(conn.close)
        wodb.migrate(conn)
        wo.cmd_import_scans(conn, self.out / 'pending')
        payloads = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM observations WHERE source='alert:linkedin'")]
        self.assertTrue(any(p.get('current', {}).get('company_logo_url') == logo for p in payloads))


    def test_alerts_become_presence_only_observations(self):
        (self.inbox / "0001.eml").write_bytes(alert_email())
        (self.inbox / "0002.eml").write_bytes(b"not an email with any html")
        status = sa.run(self.portals, self.out, "2026-09-23", self.inbox)
        # The junk file is not a known alert: skipped without costing the run.
        self.assertEqual((status["state"], status["messages"], status["jobs"], status["eligible"]), ("ok", 1, 3, 2))
        by_id = {v["source_id"]: v for v in self.items().values()}
        self.assertEqual(by_id["4012345678"]["source_scope"], "alert:linkedin")
        self.assertTrue(by_id["4012345678"]["current"]["eligible"])
        self.assertFalse(by_id["4011111111"]["current"]["eligible"])
        self.assertEqual(by_id["4099999999"]["current"]["posted"], "")
        self.assertEqual(by_id["4099999999"]["current"]["alert_date"], "2026-09-23")
        # A later run with no new mail says nothing about those adverts: alerts never prove absence.
        sa.run(self.portals, self.out, "2026-09-26", self.inbox)
        again = sa.run(self.portals, self.out, "2026-09-27", self.inbox)
        self.assertEqual(again["messages"], 0)
        self.assertTrue(all(v["source_lifecycle"] == "active" for v in self.items().values()))

    def test_mail_from_other_senders_is_ignored(self):
        (self.inbox / "0001.eml").write_bytes(alert_email(sender="Someone <someone@example.test>"))
        self.assertEqual(sa.run(self.portals, self.out, "2026-09-23", self.inbox)["messages"], 0)

    def test_disabled_alerts_record_nothing(self):
        self.portals.write_text(PORTALS.replace("enabled: true", "enabled: false"))
        self.assertEqual(sa.run(self.portals, self.out, "2026-09-23", self.inbox)["state"], "disabled")
        self.assertFalse((self.out / "pending" / "observations.json").exists())

    def test_missing_credentials_are_an_isolated_config_failure(self):
        status = sa.run(self.portals, self.out, "2026-09-23", None, env={})
        self.assertEqual(status["state"], "config")
        self.assertIn("WO_ALERTS_IMAP_HOST", status["detail"])
        source = json.loads((self.out / "pending" / "observations.json").read_text())["sources"]["alert:linkedin"]
        self.assertEqual((source["state"], source["complete"]), ("config", False))

    def state_path(self):
        return self.out / "pending" / "alerts" / "state.json"

    def test_capped_backlog_processes_oldest_messages_then_finishes(self):
        for i in range(205):
            (self.inbox / f"{i:04}.eml").write_bytes(alert_email())
        first = sa.run(self.portals, self.out, "2026-09-23", self.inbox)
        self.assertEqual((first["state"], first["complete"], first["messages"]), ("partial", False, 200))
        self.assertEqual(json.loads(self.state_path().read_text())["last_uid"], 200)
        second = sa.run(self.portals, self.out, "2026-09-24", self.inbox)
        self.assertEqual((second["state"], second["messages"]), ("ok", 5))
        self.assertEqual(json.loads(self.state_path().read_text())["last_uid"], 205)

    def test_unknown_known_sender_layout_is_retried_without_skipping_later_mail(self):
        for uid, html in [(1, HTML), (2, "<p>New LinkedIn layout</p>"), (3, HTML)]:
            (self.inbox / f"{uid}.eml").write_bytes(alert_email(html))
        first = sa.run(self.portals, self.out, "2026-09-23", self.inbox)
        self.assertEqual((first["state"], first["complete"], first["failed_uid"]), ("error", False, 2))
        self.assertEqual(json.loads(self.state_path().read_text())["last_uid"], 1)
        again = sa.run(self.portals, self.out, "2026-09-24", self.inbox)
        self.assertEqual(again["failed_uid"], 2)
        (self.inbox / "2.eml").write_bytes(alert_email())
        recovered = sa.run(self.portals, self.out, "2026-09-25", self.inbox)
        self.assertEqual((recovered["state"], recovered["messages"]), ("ok", 2))

    def test_corrupt_checkpoint_fails_visibly_without_replacement(self):
        for content in ["{broken", '[]', '{"last_uid": -1}', '{"last_uid": "12", "uidvalidity": "3"}']:
            with self.subTest(content=content):
                self.state_path().parent.mkdir(parents=True, exist_ok=True)
                self.state_path().write_text(content)
                result = sa.run(self.portals, self.out, "2026-09-23", self.inbox)
                self.assertEqual(result["state"], "error")
                self.assertIn("checkpoint", result["detail"])
                self.assertEqual(self.state_path().read_text(), content)

    def test_observation_write_failure_does_not_acknowledge_mail(self):
        (self.inbox / "1.eml").write_bytes(alert_email())
        with patch.object(sa.ObservationStore, "observe", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sa.run(self.portals, self.out, "2026-09-23", self.inbox)
        self.assertFalse(self.state_path().exists())
        self.assertEqual(sa.run(self.portals, self.out, "2026-09-24", self.inbox)["messages"], 1)

    def test_evidence_sync_failure_does_not_acknowledge_mail(self):
        (self.inbox / "1.eml").write_bytes(alert_email())
        with patch.object(sa, "_flush", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sa.run(self.portals, self.out, "2026-09-23", self.inbox)
        self.assertFalse(self.state_path().exists())

    def test_server_exception_never_discloses_credentials(self):
        env = dict(zip(sa.ENV_KEYS, ["imap.example.test", "private@example.test", "private-password"]))
        for error_type in (RuntimeError, KeyError, ValueError):
            with self.subTest(error_type=error_type), patch.object(sa, "fetch_imap", side_effect=error_type("private-password private@example.test")):
                status = sa.run(self.portals, self.out, "2026-09-23", env=env)
            self.assertEqual(status["state"], "error")
            self.assertNotIn("private-password", json.dumps(status))
            self.assertNotIn("private@example.test", (self.out / "pending" / "observations.json").read_text())

    def test_credential_directory_is_supported_without_environment_secrets(self):
        folder = Path(self.tmp.name) / "credentials"
        folder.mkdir()
        for key, value in zip(sa.ENV_KEYS, ["imap.example.test", "user", "password"]):
            (folder / key).write_text(value)
        with patch.object(sa, "fetch_imap", return_value=([], {"uidvalidity": "9", "last_uid": 0})) as fetch:
            result = sa.run(self.portals, self.out, "2026-09-23", env={"CREDENTIALS_DIRECTORY": str(folder)})
        self.assertEqual(result["state"], "empty")
        self.assertEqual(fetch.call_args.args[1][sa.ENV_KEYS[2]], "password")


class LayoutTests(unittest.TestCase):
    def test_observed_linkedin_nested_table_cards_with_repeated_job_links(self):
        # Structural shape checked against three real October alerts. Synthetic titles,
        # employers and IDs only; no message contents, personal data or tracking tokens.
        cards = []
        for jid, title, company, location, badge in [
            ("900001", "Engineering Lead", "Example Alpha", "London (Hybrid)", "1 company alum"),
            ("900002", "Director of Engineering", "Example Beta", "London (Remote)", "Easy Apply"),
        ]:
            link = f"https://www.linkedin.com/comm/jobs/view/{jid}/"
            cards.append(f'''<table><tbody><tr><td><a href="{link}"><img></a></td>
              <td><a href="{link}"><table><tbody><tr><td><a href="{link}">{title}</a></td></tr>
              <tr><td><p>{company} &middot; {location}</p></td></tr>
              <tr><td><table><tr><td><img></td><td><p>{badge}</p></td></tr></table></td></tr>
              </tbody></table></a></td></tr></tbody></table>''')
        kind, jobs, alert_date = sa.read_message(alert_email("".join(cards)), {"linkedin": ["jobalerts-noreply@linkedin.com"]})
        self.assertEqual((kind, alert_date), ("linkedin", "2026-09-23"))
        self.assertEqual(jobs, [
            dict(source_id="900001", title="Engineering Lead", company="Example Alpha", location="London (Hybrid)", url="https://www.linkedin.com/jobs/view/900001/"),
            dict(source_id="900002", title="Director of Engineering", company="Example Beta", location="London (Remote)", url="https://www.linkedin.com/jobs/view/900002/"),
        ])

    def test_inline_card_metadata_keeps_company_and_location_together(self):
        html = '<a href="https://www.linkedin.com/jobs/view/12345">Engineering Lead</a><p><span>Example Ltd</span><span> · </span><span>London</span></p>'
        job = sa.parse_linkedin(html)[0]
        self.assertEqual((job["company"], job["location"]), ("Example Ltd", "London"))

    def test_split_title_company_link_and_separate_location(self):
        html = '<a href="https://www.linkedin.com/jobs/view/engineering-lead-12345"><strong>Engineering</strong> Lead</a><p><a href="https://www.linkedin.com/company/example">Example Ltd</a></p><p>London, United Kingdom</p>'
        self.assertEqual(sa.parse_linkedin(html), [dict(source_id="12345", title="Engineering Lead", company="Example Ltd", location="London, United Kingdom", url="https://www.linkedin.com/jobs/view/12345/")])

    def test_lookalikes_credentials_and_url_text_are_not_job_links(self):
        for url in ["https://linkedin.com.evil.test/jobs/view/123", "https://evil.test/linkedin.com/jobs/view/123", "https://linkedin.com@evil.test/jobs/view/123", "https://user@www.linkedin.com/jobs/view/123", "javascript:https://www.linkedin.com/jobs/view/123", "https://www.linkedin.com:444/jobs/view/123"]:
            with self.subTest(url=url):
                self.assertEqual(sa.parse_linkedin(f'<a href="{url}">Engineering Lead</a><p>Example · London</p>'), [])

    def test_known_sender_with_unreadable_html_fails_not_empty_success(self):
        with self.assertRaises(ValueError):
            sa.read_message(alert_email("<p>Changed layout</p>"), {"linkedin": ["jobalerts-noreply@linkedin.com"]})

    def test_missing_title_does_not_acknowledge_partial_message(self):
        with self.assertRaises(ValueError):
            sa.read_message(alert_email(HTML + '<a href="https://www.linkedin.com/jobs/view/123">View job</a>'), {"linkedin": ["jobalerts-noreply@linkedin.com"]})


class FakeIMAP:
    def __init__(self, uids, fail_uid=None, validity=b"9", fail_step=None):
        self.uids, self.fail_uid, self.validity, self.fail_step = uids, fail_uid, validity, fail_step
        self.calls = []

    def login(self, *args):
        return ("NO" if self.fail_step == "login" else "OK"), [b"private-password"]

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return ("NO" if self.fail_step == "select" else "OK"), [b"1"]

    def response(self, name):
        return name, [self.validity]

    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == "SEARCH":
            return ("NO" if self.fail_step == "search" else "OK"), [b" ".join(str(u).encode() for u in self.uids)]
        if int(args[0]) == self.fail_uid:
            return "NO", [b"private-password"]
        return "OK", [(b"body", alert_email())]

    def logout(self):
        self.closed = True


class IMAPTests(unittest.TestCase):
    def fetch(self, fake, state=None):
        with patch("imaplib.IMAP4_SSL", return_value=fake) as constructor:
            result = sa.fetch_imap({"senders": {"linkedin": ["jobalerts-noreply@linkedin.com"]}}, dict(zip(sa.ENV_KEYS, ["imap.example.test", "user", "private-password"])), state or {}, "2026-09-23")
        self.assertGreater(constructor.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(constructor.call_args.kwargs["timeout"], 60)
        self.assertTrue(fake.readonly)
        self.assertTrue(fake.closed)
        return result

    def test_oldest_200_selected_and_checkpoint_is_not_prematurely_advanced(self):
        fake = FakeIMAP(list(range(205, 0, -1)))
        messages, receipt = self.fetch(fake)
        self.assertEqual([u for u, _ in messages], list(range(1, 201)))
        self.assertEqual(receipt["last_uid"], 0)
        self.assertTrue(receipt["capped"])
        self.assertTrue(all(args[1] == "(BODY.PEEK[])" for command, args in fake.calls if command == "FETCH"))

    def test_failed_fetch_returns_only_successful_prefix_for_retry(self):
        messages, receipt = self.fetch(FakeIMAP([1, 2, 3], fail_uid=2))
        self.assertEqual([u for u, _ in messages], [1])
        self.assertEqual(receipt["failed_uid"], 2)
        self.assertNotIn("private-password", json.dumps(receipt))

    def test_uidvalidity_reset_restarts_window_and_same_validity_resumes(self):
        messages, _ = self.fetch(FakeIMAP([1, 2]), {"uidvalidity": "old", "last_uid": 99})
        self.assertEqual([u for u, _ in messages], [1, 2])
        messages, _ = self.fetch(FakeIMAP([1, 2]), {"uidvalidity": "9", "last_uid": 1})
        self.assertEqual([u for u, _ in messages], [2])

    def test_first_message_failure_cannot_age_out_of_retry_window(self):
        fake = FakeIMAP([1, 2])
        self.fetch(fake, {"uidvalidity": "9", "last_uid": 0, "first_since": "2026-01-01"})
        search = next(args for command, args in fake.calls if command == "SEARCH")
        self.assertEqual(search[:2], ("UID", "1:*"))
        self.assertEqual(search[2:4], ("SINCE", "01-Jan-2026"))

    def test_fetch_time_budget_returns_prefix_for_durable_checkpoint(self):
        with patch.object(sa.time, "monotonic", side_effect=[0, 1, sa.FETCH_SECONDS + 1]):
            messages, receipt = self.fetch(FakeIMAP([1, 2, 3]))
        self.assertEqual([u for u, _ in messages], [1])
        self.assertTrue(receipt["capped"])

    def test_failed_protocol_steps_and_missing_validity_fail_safely(self):
        for step in ["login", "select", "search"]:
            with self.subTest(step=step), self.assertRaises(RuntimeError) as raised:
                self.fetch(FakeIMAP([1], fail_step=step))
            self.assertNotIn("private-password", str(raised.exception))
        with self.assertRaises(RuntimeError):
            self.fetch(FakeIMAP([1], validity=None))


if __name__ == "__main__":
    unittest.main()
