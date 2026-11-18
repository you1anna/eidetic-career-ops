"""Synthetic local-spool reader contracts; no mailbox, server or real mail."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_alerts as sa
from test_scan_alerts import HTML, PORTALS, alert_email, repeated_cards_html


class FakeSpool:
    def __init__(self):
        self.rows = []
        self.store_id = "synthetic-store"

    def add(self, raw=None, received_at="2026-09-23T06:00:00Z"):
        raw = alert_email() if raw is None else raw
        row = dict(sequence=len(self.rows) + 1, receipt_id=hashlib.sha256(raw).hexdigest(),
                   received_at=received_at, raw=raw, envelope_from="sender@example.test",
                   envelope_to="alerts@example.com")
        self.rows.append(row)
        return row

    def info(self):
        return dict(store_id=self.store_id, count=len(self.rows), bytes=sum(len(x['raw']) for x in self.rows),
                    max_sequence=max((x['sequence'] for x in self.rows), default=0))

    def list_messages(self, after=0, limit=200):
        return [dict(x) for x in self.rows if x['sequence'] > after][:limit]


class SpoolReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / "output"
        self.portals = self.root / "portals.yml"
        self.portals.write_text(PORTALS.replace("enabled: true", "enabled: true\n  transport: spool"))
        self.spool = FakeSpool()
        self.env = {"WO_ALERTS_SPOOL": str(self.root / "mail.db")}
        def factory(path, readonly=False):
            self.assertTrue(readonly)
            self.assertEqual(Path(path), Path(self.env["WO_ALERTS_SPOOL"]))
            return self.spool
        self.mock_module = patch.dict(sys.modules, {"mail_intake": types.SimpleNamespace(MailSpool=factory)})
        self.mock_module.start()
        self.addCleanup(self.mock_module.stop)

    def run_scan(self, **kwargs):
        return sa.run(self.portals, self.out, "2026-10-07", env=self.env, **kwargs)

    @property
    def checkpoint(self):
        return self.out / "pending/alerts/spool-state.json"

    def observations(self):
        return json.loads((self.out / "pending/observations.json").read_text())

    def test_receipt_time_survives_replay_and_empty_reads(self):
        row = self.spool.add()
        self.assertEqual(self.run_scan()["state"], "ok")
        first = self.observations()["items"]
        for item in first.values():
            self.assertEqual(item["last_seen_at"], row["received_at"])
            self.assertEqual(item["current"]["alert_date"], "2026-09-23")
            self.assertEqual(item["current"]["posted"], "")
        self.checkpoint.unlink()  # safe, attended replay of retained source
        self.assertEqual(self.run_scan()["new"], 0)
        self.assertEqual(self.observations()["items"], first)
        self.assertEqual(self.run_scan()["state"], "empty")
        self.assertEqual(self.observations()["items"], first)
        self.assertEqual(json.loads(self.checkpoint.read_text())["last_receipt_id"], row["receipt_id"])
        self.assertIn("2026-09-23\t", (self.out / "pending/alerts/2026-10-07.tsv").read_text())

    def test_logo_counters_count_each_alert_and_replay_preserves_source_time(self):
        logo = 'https://media.licdn.com/dms/image/v2/ID/company-logo_100_100/0/1'
        html = HTML.replace('<img alt="Formula.">', f'<img src="{logo}" alt="Formula.">')
        self.spool.add(alert_email(html))
        later = self.spool.add(alert_email(html, date='Thu, 24 Sep 2026 07:00:00 +0100'),
                               received_at='2026-09-24T06:00:00Z')
        status = self.run_scan()
        self.assertEqual((status['cards'], status['logo_cards'], status['logo_expiry_unknown'], status['jobs']),
                         (6, 2, 2, 3))
        current = self.observations()['items']['alert:linkedin\x1f4012345678']
        self.assertEqual(current['current']['company_logo_url'], logo)
        self.assertEqual(current['last_seen_at'], later['received_at'])
        self.checkpoint.unlink()
        self.run_scan()
        self.assertEqual(self.observations()['items']['alert:linkedin\x1f4012345678'], current)

    def test_repeated_cards_keep_counts_while_spool_imports_one_job_and_original_receipt(self):
        receipt = self.spool.add(alert_email(repeated_cards_html()))
        status = self.run_scan()
        self.assertEqual((status['cards'], status['logo_cards'], status['jobs']), (3, 2, 2))
        items = self.observations()['items']
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item['last_seen_at'] == receipt['received_at'] for item in items.values()))
        self.checkpoint.unlink()
        self.run_scan()
        self.assertEqual(self.observations()['items'], items)


    def test_malformed_known_sender_is_quarantined_and_later_mail_proceeds(self):
        bad = self.spool.add(alert_email("<p>unsupported private body</p>"))
        self.spool.add()
        result = self.run_scan()
        self.assertEqual((result["state"], result["complete"], result["jobs"]), ("error", False, 3))
        self.assertEqual(result["quarantined_receipts"], [bad["receipt_id"]])
        self.assertEqual(json.loads(self.checkpoint.read_text())["last_sequence"], 2)
        again = self.run_scan()
        self.assertEqual((again["state"], again["quarantined"]), ("error", 1))
        self.assertNotIn("private body", json.dumps(again))
        with patch.object(sa, "read_message", return_value=("linkedin", sa.parse_linkedin(HTML), "2026-09-23")):
            recovered = self.run_scan(retry_quarantine=True)
        self.assertEqual((recovered["state"], recovered["quarantined"]), ("ok", 0))

    def test_unknown_sender_skips_without_quarantine(self):
        self.spool.add(alert_email(sender="Other <other@example.test>"))
        status = self.run_scan()
        self.assertEqual((status["state"], status["skipped"], status["quarantined"]), ("empty", 1, 0))
        self.assertEqual(json.loads(self.checkpoint.read_text())["last_sequence"], 1)

    def test_capped_prefix_replays_remaining_mail(self):
        self.portals.write_text(self.portals.read_text().replace("transport: spool", "transport: spool\n  max_messages: 1"))
        self.spool.add()
        self.spool.add(received_at="2026-09-24T06:00:00Z")
        first = self.run_scan()
        self.assertEqual((first["state"], first["complete"]), ("partial", False))
        self.assertEqual(self.run_scan()["state"], "ok")
        self.assertTrue(all(x['last_seen_at'] == "2026-09-24T06:00:00Z" for x in self.observations()['items'].values()))

    def test_restore_identity_or_anchor_mismatch_never_advances_cursor(self):
        for mutation in ("identity", "anchor", "regression"):
            with self.subTest(mutation=mutation):
                self.spool.rows = []
                self.spool.store_id = "synthetic-store"
                self.spool.add()
                if self.checkpoint.exists():
                    self.checkpoint.unlink()
                self.run_scan()
                before = self.checkpoint.read_bytes()
                if mutation == "identity":
                    self.spool.store_id = "restored-store"
                elif mutation == "anchor":
                    self.spool.rows[0]["receipt_id"] = "0" * 64
                else:
                    self.spool.rows = []
                self.assertEqual(self.run_scan()["state"], "error")
                self.assertEqual(self.checkpoint.read_bytes(), before)

    def test_corrupt_checkpoint_fails_closed_without_leaking_content(self):
        self.spool.add()
        self.checkpoint.parent.mkdir(parents=True)
        self.checkpoint.write_text('private-body-not-json')
        result = self.run_scan()
        self.assertEqual(result['state'], 'error')
        self.assertNotIn('private-body', json.dumps(result))
        self.assertEqual(self.checkpoint.read_text(), 'private-body-not-json')

    def test_failed_durable_stage_does_not_checkpoint(self):
        self.spool.add()
        with patch.object(sa, '_flush', side_effect=OSError('private-secret')):
            result = self.run_scan()
        self.assertEqual(result['state'], 'error')
        self.assertFalse(self.checkpoint.exists())
        self.assertNotIn('private-secret', json.dumps(result))
        self.assertEqual(self.run_scan()['state'], 'ok')

    def test_failed_quarantine_write_does_not_pass_bad_mail(self):
        self.spool.add(alert_email('<p>unreadable</p>'))
        self.spool.add()
        with patch.object(sa, 'atomic_write', side_effect=OSError('private-secret')):
            result = self.run_scan()
        self.assertEqual(result['state'], 'error')
        self.assertFalse(self.checkpoint.exists())

    def test_probe_has_no_output_files_or_checkpoint_effects(self):
        self.spool.add()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sa.probe(self.portals, '2026-10-07', env=self.env), 0)
        self.assertFalse(self.out.exists())
        self.run_scan()
        before = {p: p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sa.probe(self.portals, '2026-10-07', env=self.env), 0)
        self.assertEqual({p: p.read_bytes() for p in self.out.rglob('*') if p.is_file()}, before)

    def test_missing_or_relative_spool_is_a_config_failure(self):
        for value in ('', 'relative.db'):
            with self.subTest(value=value):
                self.env['WO_ALERTS_SPOOL'] = value
                self.assertEqual(self.run_scan()['state'], 'config')

    def test_absolute_config_path_works_without_a_new_credential(self):
        self.spool.add()
        self.portals.write_text(self.portals.read_text() + f'  spool_path: {self.env["WO_ALERTS_SPOOL"]}\n')
        self.assertEqual(sa.run(self.portals, self.out, '2026-10-07', env={})['state'], 'ok')

    def test_private_quarantine_records_references_without_raw_mail(self):
        self.spool.add(alert_email('<p>private-body</p>'))
        self.run_scan()
        path = self.checkpoint.with_name('spool-quarantine.json')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn('private-body', path.read_text())
        self.assertNotIn('raw', path.read_text())

    def test_time_budget_yields_a_partial_receipt_without_consuming(self):
        self.spool.add()
        with patch.object(sa.time, 'monotonic', side_effect=[0, sa.FETCH_SECONDS + 1]):
            result = self.run_scan()
        self.assertEqual((result['state'], result['processed']), ('partial', 0))
        self.assertEqual(json.loads(self.checkpoint.read_text())['last_sequence'], 0)

    def test_quarantine_resolution_survives_interrupted_checkpoint_write(self):
        self.spool.add(alert_email('<p>unsupported</p>'))
        self.run_scan()
        before = self.checkpoint.read_bytes()
        original = sa._save_spool_ledger
        def fail_checkpoint(path, payload):
            if path == self.checkpoint:
                raise OSError('synthetic interruption')
            return original(path, payload)
        with patch.object(sa, 'read_message', return_value=('linkedin', sa.parse_linkedin(HTML), '2026-09-23')), \
             patch.object(sa, '_save_spool_ledger', side_effect=fail_checkpoint):
            self.assertEqual(self.run_scan(retry_quarantine=True)['state'], 'error')
        self.assertEqual(self.checkpoint.read_bytes(), before)
        self.assertEqual(self.run_scan()['state'], 'empty')
        self.assertEqual(len(self.observations()['items']), 3)

    def test_lost_quarantine_ledger_cannot_silently_clear_a_failure(self):
        self.spool.add(alert_email('<p>unsupported</p>'))
        self.run_scan()
        self.checkpoint.with_name('spool-quarantine.json').unlink()
        self.assertEqual(self.run_scan()['state'], 'error')

    def test_old_quarantine_recovery_cannot_overwrite_newer_evidence(self):
        self.spool.add(alert_email('<p>unsupported</p>'))
        self.spool.add(received_at='2026-10-01T06:00:00Z')
        self.run_scan()
        before = self.observations()['items']
        with patch.object(sa, 'read_message', return_value=('linkedin', sa.parse_linkedin(HTML), '2026-09-23')):
            self.run_scan(retry_quarantine=True)
        self.assertEqual(self.observations()['items'], before)

    def test_source_growth_during_read_waits_for_the_next_run(self):
        self.spool.add()
        original = self.spool.list_messages
        added = False
        def concurrent_delivery(after=0, limit=200):
            nonlocal added
            if not added:
                self.spool.add(received_at='2026-10-01T06:00:00Z')
                added = True
            return original(after, limit)
        with patch.object(self.spool, 'list_messages', side_effect=concurrent_delivery):
            result = self.run_scan()
        self.assertEqual((result['state'], result['processed'], result['high_watermark']), ('ok', 1, 1))
        self.assertEqual(self.run_scan()['processed'], 1)


if __name__ == '__main__':
    unittest.main()
