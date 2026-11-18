"""HTTP contract for the private database-only workspace."""
import json
import gzip
import os
import re
import shutil
import socket
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import unittest.mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db
import records
import workspace_state
import server
import states


_SEEDED = tempfile.TemporaryDirectory(prefix='wo-seeded-')
_seeded = {}


def seeded_copy(name, build, destination):
    """Build each seeded database once per process; every test writes to its own copy."""
    if name not in _seeded:
        template = Path(_SEEDED.name) / f'{name}.db'
        build(template)
        _seeded[name] = template
    shutil.copyfile(_seeded[name], destination)
    return destination


def migrated_contract_copy(source_path,destination):
    """Only the test copy is upgraded; opening the source must not alter schema or file settings."""
    source=sqlite3.connect(Path(source_path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        db.backup(source,destination)
    finally:
        source.close()
    copy=db.connect(destination)
    try:
        db.migrate(copy)
    finally:
        copy.close()


class ContractCopyTests(unittest.TestCase):
    def test_older_database_is_migrated_only_on_its_disposable_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=root/'migrations';old.mkdir()
            for migration in db.MIGRATIONS.glob('*.sql'):
                if int(migration.name[:3])<7:
                    (old/migration.name).write_text(migration.read_text())
            original=root/'original.db';source=db.connect(original)
            db.migrate(source,old)
            source.execute("INSERT INTO records(id,type,name,created_at,updated_at) VALUES('P1','person','Example','x','x')")
            source.execute("INSERT INTO drafts(record_id,title,text) VALUES('P1','Note','Saved text')")
            source.close()
            before=original.read_bytes()
            copied=root/'copy.db';migrated_contract_copy(original,copied)
            self.assertEqual(original.read_bytes(),before)
            check=db.connect(copied)
            try:
                self.assertFalse(db.pending(check))
                self.assertEqual(check.execute('SELECT text FROM draft_versions').fetchone()[0],'Saved text')
                self.assertEqual(records.state(check)['records'][0]['name'],'Example')
            finally:
                check.close()


class BrowserConnectionBurstTests(unittest.TestCase):
    def test_module_load_connections_survive_a_brief_accept_pause(self):
        # A browser opens parallel module connections; navigation can overlap the previous load.
        # Queue the burst before accepting, making the old five-connection backlog fail reliably.
        http = server.WorkspaceHTTPServer(("127.0.0.1", 0), server.Handler)
        clients = []
        worker = None
        try:
            for _ in range(12):
                clients.append(socket.create_connection(http.server_address, timeout=0.5))
            worker = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()
            for client in clients:
                client.settimeout(2)
                client.sendall((f"GET /js/dom.js HTTP/1.0\r\nHost: 127.0.0.1:{http.server_port}\r\n\r\n").encode())
            for client in clients:
                response = b""
                while chunk := client.recv(65536):
                    response += chunk
                self.assertTrue(response.startswith(b"HTTP/1.0 200"), response[:100])
                self.assertIn(b"export", response.split(b"\r\n\r\n", 1)[1])
        finally:
            for client in clients:
                client.close()
            if worker:
                http.shutdown()
                worker.join(timeout=3)
            http.server_close()


class StateCacheMemoryTests(unittest.TestCase):
    """The hosted server has a 512 MB cap: hold one full state at most and never build two at once."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "work-ops.db"
        conn = db.connect(self.path)
        db.migrate(conn)
        conn.close()
        records._STATE_CACHE.clear()
        records._JSON_CACHE.clear()

    def tearDown(self):
        records._STATE_CACHE.clear()
        records._JSON_CACHE.clear()
        self.temp.cleanup()

    def commit_elsewhere(self, name):
        other = db.connect(self.path)
        try:
            records.create(other, {'type': 'company', 'name': name}, actor='agent')
        finally:
            other.close()

    def test_summary_bytes_are_reused_until_the_data_changes_and_no_summary_dict_is_kept(self):
        conn = db.connect(self.path)
        try:
            raw, gz = records.cached_summary_json(conn)
            self.assertIs(records.cached_summary_json(conn)[0], raw)
            self.assertEqual(json.loads(gzip.decompress(gz)), json.loads(raw))
            self.assertEqual(json.loads(raw)['records'], [])
            self.assertNotIn(True, records._STATE_CACHE)
            self.commit_elsewhere('Example Ltd')
            changed, _ = records.cached_summary_json(conn)
            self.assertEqual([r['name'] for r in json.loads(changed)['records']], ['Example Ltd'])
        finally:
            conn.close()

    def test_a_rebuild_compresses_nothing_until_a_full_summary_is_requested(self):
        """A save is answered with a delta: compress a full form once, and only when one is served."""
        conn = db.connect(self.path)
        try:
            with unittest.mock.patch.object(workspace_state.gzip, 'compress', wraps=gzip.compress) as compress:
                first = records.summary_json(conn)
                self.assertEqual(compress.call_count, 1)
                self.assertIs(records.summary_json(conn)[1], first[1])
                self.assertEqual(compress.call_count, 1)
                base = json.loads(first[0])['version']
                self.commit_elsewhere('Example Ltd')
                delta, compressed = records.summary_json(conn, base)
                self.assertTrue(json.loads(delta)['delta'])
                self.assertIsNone(compressed)
                self.assertEqual(compress.call_count, 1)  # the rebuild itself compressed nothing
                packed = records.summary_json(conn, encoding='strings-v1')
                self.assertEqual(compress.call_count, 2)
                self.assertIs(records.summary_json(conn, encoding='strings-v1')[0], packed[0])
                plain = records.summary_json(conn)
                self.assertEqual(compress.call_count, 3)
            self.assertEqual(gzip.decompress(plain[1]), plain[0])
            self.assertEqual(gzip.decompress(packed[1]), packed[0])
            self.assertEqual(json.loads(plain[0])['version'], json.loads(delta)['version'])
        finally:
            conn.close()

    def test_one_build_reads_the_exclusion_policy_once(self):
        """Records and both decision projections share one policy read inside the build's snapshot."""
        import decision_views
        import record_read
        import record_views
        conn = db.connect(self.path)
        try:
            records.create(conn, {'type': 'company', 'name': 'Example Ltd'}, actor='agent')
            records.create_decision(conn, dict(subject='workspace:example', kind='approve', question='Proceed?'))
            # Each consumer resolves the name through its own module: count all three routes.
            with unittest.mock.patch.object(record_read, 'record_policy', wraps=record_read.record_policy) as policy, \
                    unittest.mock.patch.object(record_views, 'record_policy', policy), \
                    unittest.mock.patch.object(decision_views.record_read, 'record_policy', policy):
                state = records.state(conn, compact=True)
            self.assertEqual(policy.call_count, 1)
            self.assertEqual([r['name'] for r in state['records']], ['Example Ltd'])
            self.assertEqual(len(state['decisions']), 1)
        finally:
            conn.close()

    def test_attention_reads_are_batched_and_do_not_write(self):
        conn = db.connect(self.path)
        try:
            counts = []
            for size in (5, 50):
                for n in range(size):
                    records.create(conn, {'type': 'person', 'name': f'Scale {size}-{n}', 'relationship': 'warm'}, actor='agent')
                changes = conn.total_changes
                queries = []
                conn.set_trace_callback(queries.append)
                view = records.state(conn, compact=True)
                conn.set_trace_callback(None)
                counts.append(sum(q.lstrip().upper().startswith('SELECT') for q in queries))
                self.assertEqual(conn.total_changes, changes)
                self.assertFalse(any(q.lstrip().split(' ', 1)[0].upper() in ('INSERT', 'UPDATE', 'DELETE') for q in queries))
                self.assertNotIn('attention', view['intelligence'])
                self.assertTrue({'act_now', 'waiting', 'in_progress', 'needs_checking'} <= set(view['opportunities']))
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM week_items').fetchone()[0], 0)
            self.assertEqual(counts[0], counts[1], 'attention cannot add per-person queries')
        finally:
            conn.close()

    def test_simultaneous_cold_requests_build_the_state_once(self):
        built = []
        real = records.state
        start = threading.Barrier(4)

        def slow_state(conn, compact=False):
            built.append(compact)
            threading.Event().wait(0.2)
            return real(conn, compact)

        def request():
            conn = db.connect(self.path)
            try:
                start.wait()
                records.cached_state(conn)
            finally:
                conn.close()

        with unittest.mock.patch.object(workspace_state, 'state', slow_state):
            threads = [threading.Thread(target=request) for _ in range(4)]
            for t in threads: t.start()
            for t in threads: t.join()
        self.assertEqual(built, [False])

    def test_a_waiting_summary_reuses_the_current_build_after_a_concurrent_commit(self):
        import concurrent.futures
        key_read = threading.Event()
        real_key = workspace_state._data_key
        caller = threading.current_thread()

        def observed_key(conn, compact):
            key = real_key(conn, compact)
            if threading.current_thread() is not caller:
                key_read.set()
            return key

        def request():
            conn = db.connect(self.path)
            try:
                return records.summary_json(conn)[0]
            finally:
                conn.close()

        conn = db.connect(self.path)
        try:
            with unittest.mock.patch.object(workspace_state, '_data_key', side_effect=observed_key), \
                 unittest.mock.patch.object(workspace_state, 'state', wraps=workspace_state.state) as build, \
                 concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                # The waiter reads the old key, then waits for the build lock. Another request
                # builds the newly committed state before the waiter acquires that lock.
                with workspace_state._BUILD_LOCK:
                    waiting = pool.submit(request)
                    self.assertTrue(key_read.wait(5), 'waiting request did not read its cache key')
                    self.commit_elsewhere('Current company')
                    current = records.summary_json(conn)[0]
                waited = waiting.result(timeout=5)
                following = records.summary_json(conn)[0]
                self.assertEqual(build.call_count, 1, 'the same committed state must build once')
                self.assertIs(waited, current)
                self.assertIs(following, current)
                self.assertEqual([r['name'] for r in json.loads(current)['records']], ['Current company'])
        finally:
            conn.close()

    def test_an_uncommitted_summary_bypasses_the_shared_committed_cache(self):
        conn = db.connect(self.path)
        try:
            committed = records.summary_json(conn)[0]
            conn.execute('BEGIN IMMEDIATE')
            records.create(conn, {'type': 'company', 'name': 'Uncommitted company'}, actor='agent')
            uncommitted = records.summary_json(conn)[0]
            self.assertTrue(conn.in_transaction)
            self.assertEqual([r['name'] for r in json.loads(uncommitted)['records']], ['Uncommitted company'])
            # Existing policy releases the old cache before any build, including an uncached
            # caller-owned transaction. Its private result must never enter the shared cache.
            self.assertNotIn(True, records._JSON_CACHE)
            conn.rollback()
            restored = records.summary_json(conn)[0]
            self.assertEqual(json.loads(restored)['records'], [])
            self.assertEqual(json.loads(committed)['records'], [])
        finally:
            conn.close()

    def test_the_stale_full_state_is_released_before_its_replacement_is_built(self):
        conn = db.connect(self.path)
        try:
            records.cached_state(conn)
            self.commit_elsewhere('Example Ltd')
            seen = []
            real = records.state

            def watching_state(c, compact=False):
                seen.append(False in records._STATE_CACHE)
                return real(c, compact)

            with unittest.mock.patch.object(workspace_state, 'state', watching_state):
                records.cached_state(conn)
            self.assertEqual(seen, [False])
        finally:
            conn.close()

    def test_concurrent_transport_requests_encode_once_without_database_writes(self):
        import concurrent.futures
        import summary_transport
        barrier = threading.Barrier(4)
        def request():
            conn = db.connect(self.path)
            try:
                before = conn.total_changes
                barrier.wait()
                result = records.summary_json(conn, encoding='strings-v1')
                self.assertEqual(conn.total_changes, before)
                return result
            finally:
                conn.close()
        with unittest.mock.patch.object(summary_transport, 'encode', wraps=summary_transport.encode) as encode:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: request(), range(4)))
            self.assertEqual(encode.call_count, 1)
        self.assertTrue(all(result[0] is results[0][0] for result in results))
        self.assertNotIn(True, records._STATE_CACHE)


class ServerCase(unittest.TestCase):
    """A real workspace server on a free loopback port over a temporary database."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "work-ops.db"
        conn = db.connect(self.path)
        db.migrate(conn)
        conn.close()
        self.http = server.WorkspaceHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http.db = self.path
        self.http.public_origin = None
        self.http.lan_host = None
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, payload=None, *, origin=True, headers=None):
        request_headers = dict(headers or {})
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
        if origin and "Origin" not in request_headers:
            request_headers["Origin"] = self.base
        request = Request(self.base + path, data=data, headers=request_headers, method=method)
        with urlopen(request) as response:
            kind = response.headers.get_content_type()
            return response.status, response.headers, json.load(response) if kind == "application/json" else response.read()

    def rejected(self, method, path, payload=None, *, origin=True, headers=None):
        with self.assertRaises(HTTPError) as caught:
            self.request(method, path, payload, origin=origin, headers=headers)
        error = caught.exception
        body = json.load(error)
        error.close()
        return error.code, body

    def create(self, typ="person", name="Test person", **fields):
        return self.request("POST", "/api/records", {"type": typ, "name": name, **fields})[2]["record"]


def apply_summary_delta(old, delta):
    """Reference reading of the save-update protocol; js/summary-delta.js implements the same rule."""
    assert delta['delta'] is True and delta['base'] == old['version']
    new = {k: v for k, v in old.items() if k not in delta['unset']}
    new.update(delta['set'])
    intel = {k: v for k, v in old['intelligence'].items() if k not in delta['intelligence']['unset']}
    intel.update(delta['intelligence']['set'])

    def patch(items, change):
        by_id = {item['id']: item for item in items}
        for rid in change['remove']:
            del by_id[rid]
        for item in change['upsert']:
            by_id[item['id']] = item
        return [by_id[rid] for rid in change.get('order') or [item['id'] for item in items]]

    new['records'] = patch(old['records'], delta['collections']['records'])
    intel['actions'] = patch(old['intelligence']['actions'], delta['collections']['actions'])
    intel['research'] = patch(old['intelligence']['research'], delta['collections']['research'])
    new['intelligence'] = intel
    new['version'] = delta['version']
    return new


class SummaryDeltaTests(ServerCase):
    def setUp(self):
        super().setUp()
        records._JSON_CACHE.clear()
        workspace_state._SUMMARY_INDEX.clear()

    def summary(self, since=None):
        return self.request('GET', '/api/state?view=summary' + (f'&since={since}' if since else ''), origin=False)[2]

    def revision(self, rid):
        conn = db.connect(self.path)
        try:
            return records.get(conn, rid)['revision']
        finally:
            conn.close()

    def test_the_full_summary_is_unchanged_json_with_a_trailing_version(self):
        self.create()
        self.create('job', 'Engineering Manager', url='https://example.com/a', engagement='contract')
        conn = db.connect(self.path)
        try:
            raw, gz = records.cached_summary_json(conn)
        finally:
            conn.close()
        parsed = json.loads(raw)
        self.assertEqual(json.dumps(parsed, separators=(',', ':')).encode(), raw)
        self.assertEqual(list(parsed)[-1], 'version')
        self.assertRegex(parsed['version'], r'^[0-9a-f]{8}-[0-9]+$')
        self.assertEqual(json.loads(gzip.decompress(gz)), parsed)

    def test_shared_text_transport_preserves_version_evidence_and_delta_recovery(self):
        from test_summary_transport import decode
        job = self.create('job', 'Engineering Manager', summary='Essential: experience leading software teams.',
                          url='https://example.com/a', engagement='contract')
        before = self.summary()
        path = '/api/state?view=summary'
        packed = self.request('GET', path + '&encoding=strings-v1', origin=False)[2]
        self.assertEqual(decode(packed), before)
        with urlopen(Request(self.base + path + '&encoding=strings-v1',
                             headers={'Accept-Encoding': 'gzip'})) as response:
            self.assertEqual(response.headers['Content-Encoding'], 'gzip')
            self.assertEqual(decode(json.loads(gzip.decompress(response.read()))), before)
        conn = db.connect(self.path)
        try:
            raw, _ = records.summary_json(conn, encoding='strings-v1')
            self.assertIs(records.summary_json(conn, encoding='strings-v1')[0], raw)
            self.assertNotIn(True, records._STATE_CACHE)
        finally:
            conn.close()
        self.request('PATCH', f"/api/records/{job['id']}",
                     {'revision': self.revision(job['id']), 'notes': 'Retain the quoted source'})
        delta = self.request('GET', path + f"&since={before['version']}&encoding=strings-v1", origin=False)[2]
        self.assertEqual(apply_summary_delta(decode(packed), delta), self.summary())
        recovered = self.request('GET', path + '&since=00000000-1&encoding=strings-v1', origin=False)[2]
        self.assertEqual(decode(recovered), self.summary())

    def test_each_save_sends_only_changes_that_rebuild_the_new_summary(self):
        person = self.create()
        job = self.create('job', 'Engineering Manager', url='https://example.com/a', engagement='contract',
                          location='London', summary='Lead software engineering teams.')
        self.create('job', 'Delivery Lead', url='https://example.com/c', engagement='contract')
        old = self.summary()
        self.assertTrue(old['intelligence']['actions'] or old['intelligence']['research'])
        steps = [
            lambda: self.request('PATCH', f"/api/records/{person['id']}", {'revision': self.revision(person['id']), 'notes': 'Met at a meetup'}),
            lambda: self.create('job', 'Head of Platform', url='https://example.com/b', engagement='contract'),
            lambda: self.request('POST', f"/api/records/{job['id']}/transition",
                                 {'revision': self.revision(job['id']), 'field': 'status', 'to': 'pursue'}),
            lambda: records.archive(*self.conn_for_archive(person['id'])),
        ]
        for step in steps:
            step()
            delta = self.summary(old['version'])
            new = self.summary()
            self.assertEqual(delta['version'], new['version'])
            self.assertEqual(apply_summary_delta(old, delta), new)
            self.assertLess(len(json.dumps(delta)), len(json.dumps(new)) / 2)
            old = new
        self.assertNotIn(person['id'], [r['id'] for r in old['records']])

    def conn_for_archive(self, rid):
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        return conn, rid, {'revision': records.get(conn, rid)['revision'], 'reason': 'Synthetic removal'}

    def test_an_unknown_or_evicted_base_gets_the_full_summary(self):
        first = self.summary()
        self.assertNotIn('delta', self.summary('00000000-1'))
        for i in range(workspace_state._SUMMARY_HISTORY):
            self.create(name=f'Person {i}')
            self.summary()
        full = self.summary(first['version'])
        self.assertNotIn('delta', full)
        self.assertEqual(len(full['records']), workspace_state._SUMMARY_HISTORY)
        self.assertEqual(self.rejected('GET', '/api/state?view=summary&since=../x', origin=False)[0], 404)

    def test_duplicate_ids_are_never_patched(self):
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        base = json.loads(records.cached_summary_json(conn)[0])
        self.create(name='Changes the data version')
        twin = {'records': [{'id': 'X'}, {'id': 'X'}], 'intelligence': {'actions': [], 'research': []}}
        with unittest.mock.patch.object(workspace_state, 'state', return_value=twin):
            raw, _ = records.summary_json(conn, base['version'])
        self.assertNotIn('delta', json.loads(raw))
        self.assertEqual(json.loads(raw)['records'], twin['records'])


class ApiTests(ServerCase):
    def test_message_templates_are_on_demand_without_reading_records(self):
        with unittest.mock.patch.object(server.Handler, 'with_db', side_effect=AssertionError('templates must not read the database')):
            status, _, result = self.request('GET', '/api/message-templates')
        self.assertEqual(status, 200)
        self.assertIn('former_colleague', [t['id'] for t in result['templates']])
        self.assertIn('{{personal_line}}', next(t['body'] for t in result['templates'] if t['id']=='former_colleague'))
        self.assertNotIn('records', result)

    def test_retired_draft_and_week_writes_preserve_the_database(self):
        person = self.create()
        conn = db.connect(self.path)
        try:
            records.draft(conn, dict(record_id=person['id'], revision=person['revision'],
                                    text='Saved historical wording', purpose='message'))
            before = list(conn.iterdump())
            for path, payload in [
                (f"/api/records/{person['id']}/draft", {'revision': 2, 'text': 'Replacement'}),
                ('/api/week/store', {'refresh': True}),
                ('/api/week/defer', {'action_id': 'old'}),
                ('/api/week/restore', {'action_id': 'old'}),
            ]:
                with self.subTest(path=path):
                    code, body = self.rejected('POST', path, payload)
                    self.assertEqual(code, 410)
                    self.assertIn('retired', body['error'].lower())
            self.assertEqual(list(conn.iterdump()), before)
        finally:
            conn.close()

    def test_contact_api_rejects_new_outgoing_wording(self):
        person = self.create()
        for extra in ({'sent_message': 'Copied email'}, {'draft_version_id': 1}):
            with self.subTest(extra=extra):
                code, body = self.rejected('POST', f"/api/records/{person['id']}/outcome",
                    {'kind': 'sent', 'revision': person['revision'], **extra})
                self.assertEqual(code, 410)
                self.assertIn('wording', body['error'].lower())

    def test_a_failed_save_leaves_its_cause_for_the_caretaker(self):
        locked = sqlite3.OperationalError('database is locked')
        with unittest.mock.patch.object(server.store, 'create', side_effect=locked):
            code, body = self.rejected('POST', '/api/records', {'type': 'person', 'name': 'Test person'})
        self.assertEqual(code, 500); self.assertIn('Save was not confirmed', body['error'])
        note = json.loads((self.path.parent / 'crashes' / 'work-ops-workspace.service.json').read_text())
        self.assertEqual(note['error'], 'OperationalError: database is locked')

    def test_save_job_accepts_unknown_employer_without_creating_an_organisation(self):
        payload = dict(name='Engineering Manager', url='https://example.test/unknown-employer')
        saved = self.request('POST', '/api/jobs/save', payload)[2]
        self.assertTrue(saved['created'])
        self.assertEqual(saved['record']['organisation'], '')
        self.assertEqual(saved['record']['status'], 'pursue')
        again = self.request('POST', '/api/jobs/save', dict(payload, organisation='Later claim'))[2]
        self.assertFalse(again['created'])
        self.assertEqual(again['record'], saved['record'])
        view = self.request('GET', '/api/state')[2]
        self.assertEqual(len(view['records']), 1)
        self.assertEqual(view['links'], [])
        self.assertEqual(self.rejected('POST', '/api/jobs/save', dict(url='https://example.test/no-title'))[0], 400)

    def test_save_job_and_cv_choices_use_the_normal_revision_and_origin_contract(self):
        payload = dict(name='Engineering Manager', organisation='Example', url='https://www.linkedin.com/jobs/view/manager-12345?trk=mail')
        status, _, saved = self.request('POST', '/api/jobs/save', payload)
        self.assertEqual(status, 200)
        self.assertTrue(saved['created'])
        job = saved['record']
        self.assertEqual(job['status'], 'pursue')
        self.assertEqual(self.rejected('POST', '/api/jobs/save', payload, origin=False)[0], 403)
        again = self.request('POST', '/api/jobs/save', dict(payload, notes='Must not overwrite'))[2]
        self.assertFalse(again['created'])
        self.assertEqual(again['record'], job)
        url = '/api/records/' + job['id']
        chosen = self.request('PATCH', url, dict(revision=job['revision'], cv_override='QELead'))[2]['record']
        self.assertEqual(chosen['cv_override'], 'QELead')
        self.assertEqual(self.rejected('PATCH', url, dict(revision=job['revision'], cv_override='Manager'))[0], 409)
        reset = self.request('PATCH', url, dict(revision=chosen['revision'], cv_override=None))[2]['record']
        self.assertIsNone(reset['cv_override'])
        self.assertEqual(self.rejected('POST', '/api/jobs/save', dict(payload, url='javascript:bad'))[0], 400)
        self.assertEqual(self.rejected('POST', '/api/jobs/save', dict(payload, organisation='Excludo', url='https://example.test/excluded'))[0], 409)

    def test_gzip_response_roundtrips_and_declared_length_matches_encoded_bytes(self):
        expected=(HERE/'app.js').read_bytes()
        with urlopen(Request(self.base+'/app.js',headers={'Accept-Encoding':'br, gzip;q=0.8'})) as response:
            encoded=response.read()
            self.assertEqual(response.headers['Content-Encoding'],'gzip')
            self.assertIn('Accept-Encoding',response.headers['Vary'])
            self.assertEqual(int(response.headers['Content-Length']),len(encoded))
            self.assertLess(len(encoded),len(expected))
            self.assertEqual(gzip.decompress(encoded),expected)

    def test_explicit_gzip_refusal_and_ordinary_clients_keep_the_original_response(self):
        expected=(HERE/'app.js').read_bytes()
        for accept in (None,'gzip;q=0','*;q=1, gzip;q=0'):
            with self.subTest(accept=accept):
                headers={'Accept-Encoding':accept} if accept is not None else {}
                with urlopen(Request(self.base+'/app.js',headers=headers)) as response:
                    self.assertIsNone(response.headers.get('Content-Encoding'))
                    self.assertEqual(response.read(),expected)
                    self.assertEqual(int(response.headers['Content-Length']),len(expected))

    def test_gzip_json_roundtrip_and_small_or_nontext_assets_are_unencoded(self):
        with urlopen(Request(self.base+'/api/state?view=summary',headers={'Accept-Encoding':'gzip'})) as response:
            encoded=response.read()
            self.assertEqual(response.headers['Content-Encoding'],'gzip')
            state=json.loads(gzip.decompress(encoded))
            self.assertEqual(state['mode'],'database')
            self.assertEqual(state['records'],[])
        for path in ('/api/health','/icon.svg'):
            with urlopen(Request(self.base+path,headers={'Accept-Encoding':'gzip'})) as response:
                self.assertIsNone(response.headers.get('Content-Encoding'))

    def test_the_site_asks_claude_as_owner_and_cannot_answer(self):
        r = self.create()
        saved = self.request("POST", f"/api/records/{r['id']}/question", {"question": "Prep me for Thursday"})[2]
        self.assertEqual((saved["question"]["asked_by"], saved["question"]["answer"]), ("owner", ""))
        summary = self.request("GET", "/api/state?view=summary", origin=False)[2]
        self.assertEqual([q["question"] for q in summary["questions"]], ["Prep me for Thursday"])
        self.assertEqual(summary["records"][0]["revision"], r["revision"])
        self.assertEqual(self.rejected("POST", f"/api/records/{r['id']}/question", {"question": ""})[0], 400)
        self.assertEqual(self.rejected("POST", f"/api/records/{r['id']}/question", {"question": "x"}, origin=False)[0], 403)
        self.assertEqual(self.rejected("POST", f"/api/questions/{saved['question']['id']}", {"answer": "mine"})[0], 404)

    def test_outcome_endpoint_is_revision_safe_and_never_moves_stage(self):
        r = self.create('opportunity','Example',stage='lead')
        saved = self.request('POST',f"/api/records/{r['id']}/outcome",dict(kind='won',revision=r['revision'],note='Signed engagement'))[2]
        self.assertEqual(saved['record']['stage'],'lead')
        self.assertEqual(saved['outcome']['kind'],'won')
        self.assertEqual(saved['outcome']['actor'],'owner')
        code,_ = self.rejected('POST',f"/api/records/{r['id']}/outcome",dict(kind='sent',revision=r['revision']))
        self.assertEqual(code,409)
        state=self.request('GET','/api/state',origin=False)[2]
        self.assertEqual(len(state['outcomes']),1)
        self.assertEqual(len(state['interactions']),1)

    def test_submission_requires_company_over_http_without_changing_the_record(self):
        r = self.create('person', 'Example Recruiter', kind='recruiter')
        for company in (None, '', '   '):
            code, body = self.rejected('POST', f"/api/records/{r['id']}/outcome",
                dict(kind='submitted', revision=r['revision'], company=company))
            self.assertEqual(code, 400)
            self.assertIn('company', body['error'])
        state = self.request('GET', '/api/state', origin=False)[2]
        self.assertEqual(state['outcomes'], [])
        self.assertEqual(state['records'][0]['revision'], r['revision'])
        self.assertEqual(state['interactions'], [])

    def test_outcome_endpoint_exposes_server_derived_advice_snapshot(self):
        person=self.create('person','Example friend',kind='friend',relationship='warm')
        state=self.request('GET','/api/state',origin=False)[2]
        action=next(a for a in state['intelligence']['actions'] if a['record_id']==person['id'])
        result=self.request('POST',f"/api/records/{person['id']}/outcome",dict(kind='sent',revision=person['revision'],action_id=action['id'],snapshot={'route':'fake'}))[2]
        self.assertEqual(result['outcome']['snapshot']['route'],action['route'])
        self.assertEqual(result['outcome']['snapshot']['brief'],action['brief'])
        self.assertNotIn('snapshot_json',result['outcome'])
        persisted=self.request('GET','/api/state',origin=False)[2]['outcomes'][0]
        self.assertEqual(persisted['snapshot'],result['outcome']['snapshot'])

    def test_summary_and_record_timeline_keep_all_history_accessible(self):
        person = self.create()
        other = self.create(name='Other person')
        conn = db.connect(self.path)
        for i in range(20):
            records.event(conn, person['id'], 'notes', '', f'Older detail {i}')
        records.event(conn, other['id'], 'notes', '', 'Other history')
        records.interaction(conn, dict(record_id=person['id'], revision=person['revision'], channel='meeting',
                                       direction='note', at='2026-09-30', summary='Discussed work'))
        conn.close()
        full = self.request('GET', '/api/state', origin=False)[2]
        summary = self.request('GET', '/api/state?view=summary', origin=False)[2]
        from test_records import summary_intelligence, summary_record
        self.assertEqual([summary_record(r) for r in full['records']], summary['records'])
        self.assertTrue(all(r['outcome_actions'] for r in full['records'] if r['type'] == 'person'))
        self.assertEqual(summary_intelligence(full['intelligence']), summary['intelligence'])
        self.assertEqual(len(summary['history']), 12)
        self.assertEqual(summary['interactions'], [])
        timeline = self.request('GET', f"/api/records/{person['id']}/timeline", origin=False)[2]
        self.assertEqual(len(timeline['history']), 22)
        self.assertTrue(all(e['record_id'] == person['id'] for e in timeline['history']))
        self.assertEqual(timeline['interactions'][0]['summary'], 'Discussed work')
        self.assertEqual(timeline['revision'], 2)
        self.assertEqual(self.rejected('GET', '/api/records/CP-missing/timeline', origin=False)[0], 404)

    def test_full_state_file_transport_preserves_receipts_lengths_and_gzip_negotiation(self):
        conn = db.connect(self.path)
        try:
            for n in range(25):
                conn.execute('INSERT INTO scan_runs(run_id,date,status,receipt_json) VALUES(?,?,?,?)',
                             (f'archive:{n:03}', '2020-01-01', 'ok', json.dumps({'text': 'Synthetic 🧪 ' * 200})))
            expected = records.record_views.receipts(conn)
        finally:
            conn.close()
        bodies = []
        for accept in ('gzip', 'gzip;q=0', '*;q=1, gzip;q=0'):
            with urlopen(Request(self.base + '/api/state', headers={'Accept-Encoding': accept})) as response:
                body = response.read()
                self.assertEqual(int(response.headers['Content-Length']), len(body))
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual(response.headers['Vary'], 'Accept-Encoding')
                self.assertEqual(response.headers.get('Content-Encoding'), 'gzip' if accept == 'gzip' else None)
                if accept == 'gzip': body = gzip.decompress(body)
                self.assertEqual(json.loads(body)['coverage']['runs'], expected)
                bodies.append(body)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[1], bodies[2])

    def test_create_edit_and_stale_edit_returns_409_without_lost_update(self):
        created = self.create(known_context="Initial")
        status, _, body = self.request("PATCH", f"/api/records/{created['id']}", {
            "revision": created["revision"], "known_context": "Verified context",
        })
        self.assertEqual(status, 200)
        self.assertEqual((body["record"]["known_context"], body["record"]["revision"]), ("Verified context", 2))
        code, stale = self.rejected("PATCH", f"/api/records/{created['id']}", {
            "revision": 1, "name": "Lost update",
        })
        self.assertEqual(code, 409)
        self.assertIn("Reload", stale["error"])
        state = self.request("GET", "/api/state", origin=False)[2]
        self.assertEqual(state["records"][0]["name"], "Test person")

    def test_transition_is_validated_and_history_is_visible(self):
        job = self.create("job", "Engineering Manager", status="candidate")
        transitioned = self.request("POST", f"/api/records/{job['id']}/transition", {
            "revision": job["revision"], "field": "status", "to": "pursue", "note": "Evidence reviewed",
        })[2]["record"]
        self.assertEqual((transitioned["status"], transitioned["revision"]), ("pursue", 2))
        code, _ = self.rejected("POST", f"/api/records/{job['id']}/transition", {
            "revision": 2, "field": "status", "to": "offer",
        })
        self.assertEqual(code, 400)
        state = self.request("GET", "/api/state", origin=False)[2]
        self.assertTrue(any(e["field"] == "status" and e["to_value"] == "pursue" for e in state["history"]))

    def test_same_origin_host_and_content_type_are_enforced(self):
        code, _ = self.rejected("POST", "/api/records", {"type": "person", "name": "No"}, origin=False)
        self.assertEqual(code, 403)
        code, _ = self.rejected("POST", "/api/records", {"type": "person", "name": "No"},
                                headers={"Origin": "https://evil.test"})
        self.assertEqual(code, 403)
        code, _ = self.rejected("POST", "/api/records", None,
                                headers={"Origin": self.base, "Content-Type": "text/plain"})
        self.assertEqual(code, 415)
        code, _ = self.rejected("GET", "/api/state", origin=False, headers={"Host": "evil.test"})
        self.assertEqual(code, 403)

    def test_assets_are_allowlisted_and_private_paths_are_denied(self):
        for path, kind in (("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css"),
                           ("/icon.svg", "image/svg+xml"), ("/js/api.js", "text/javascript"),
                           ("/js/dom.js", "text/javascript"), ("/js/forms.js", "text/javascript"), ("/js/outreach.js", "text/javascript")):
            status, headers, body = self.request("GET", path, origin=False)
            self.assertEqual(status, 200)
            self.assertEqual(headers.get_content_type(), kind)
            self.assertTrue(body)
            self.assertEqual(headers["Cache-Control"], "no-store")
            directives = dict(part.strip().split(' ', 1) for part in headers['Content-Security-Policy'].split(';'))
            self.assertEqual(directives['img-src'], "'self' https://media.licdn.com")
            self.assertEqual(directives['script-src'], "'self'")
        _, _, page = self.request("GET", "/", origin=False)
        self.assertIn(b'<link rel="icon" href="/icon.svg" type="image/svg+xml">', page)
        for path in ("/../../career-directory.csv", "/data/work-ops.db", "/records/private.json", "/favicon.ico", "/js/records.py"):
            code, _ = self.rejected("GET", path, origin=False)
            self.assertEqual(code, 404)

    def test_every_module_the_browser_imports_is_served(self):
        sources = [HERE / "app.js", *sorted((HERE / "js").glob("*.js"))]
        imported = {m for f in sources for m in re.findall(r'from\s+["\']\./(?:js/)?([\w-]+\.js)["\']', f.read_text())}
        self.assertGreaterEqual(len(imported), 9)
        for name in sorted(imported):
            status, headers, body = self.request("GET", f"/js/{name}", origin=False)
            self.assertEqual((status, headers.get_content_type()), (200, "text/javascript"), name)

    def test_career_ops_has_only_namespaced_public_assets(self):
        for route in ('/career-ops', '/career-ops/', '/career-ops?source=test'):
            status, _, body = self.request('GET', route, origin=False)
            self.assertEqual(status, 200)
            self.assertIn(b'https://work.example.com/career-ops', body)
            for asset in re.findall(rb'(?:href|src)="(/career-ops/[^"#]+)"', body):
                status, _, content = self.request('GET', asset.decode(), origin=False)
                self.assertEqual(status, 200)
                self.assertTrue(content)
            self.assertNotIn(b'./style.css', body)
        for route in ('/career-ops/api/state', '/career-ops/../api/state',
                      '/career-ops/%2e%2e/api/state', '/career-ops/index.html',
                      '/career-ops/private.json', '/career-ops/style.css/extra',
                      '/career-ops//style.css', '/career-ops-other'):
            self.assertEqual(self.rejected('GET', route, origin=False)[0], 404)
        self.assertEqual(self.rejected('GET', '/career-ops', origin=False,
                                      headers={'Host': 'evil.test'})[0], 403)

    def test_system_map_has_only_explicit_documentation_assets(self):
        status, _, body = self.request("GET", "/system-map", origin=False)
        self.assertEqual(status, 200)
        self.assertIn(b'How work-ops works', body)
        self.assertIn(b'Not implemented', body)
        for path in re.findall(rb'(?:href|src)="(/system-map/[^"#]+)"', body):
            status, headers, asset = self.request("GET", path.decode(), origin=False)
            self.assertEqual(status, 200, path)
            self.assertTrue(asset)
            self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
        for path in ('/docs/CLAUDE.md', '/system-map/../CLAUDE.md',
                     '/system-map/architecture.md', '/system-map/private.json'):
            self.assertEqual(self.rejected('GET', path, origin=False)[0], 404)
        self.assertEqual(self.rejected('GET', '/system-map', origin=False,
                                     headers={'Host': 'evil.test'})[0], 403)

    def test_state_returns_current_links_interactions_and_review_queue(self):
        person = self.create("person", "Past colleague", relationship="none")
        company = self.create("company", "Example company", kind="company")
        linked = self.request("POST", "/api/links", {
            "from_id": person["id"], "to_id": company["id"], "relation": "former_colleague_at",
            "revision": person["revision"], "notes": "Worked together",
        })[2]["record"]
        interaction = self.request("POST", "/api/interactions", {
            "record_id": person["id"], "revision": linked["revision"], "at": "2026-09-20",
            "channel": "email", "direction": "in", "summary": "Replied with context",
        })[2]
        csv_text = "First Name,Last Name,URL,Email Address,Company,Position,Connected On\nAlex,Example,https://www.linkedin.com/in/alex-example/,,Former Co,Engineer,01 Jan 2020\n"
        imported = self.request("POST", "/api/import/linkedin", {"csv": csv_text})[2]
        self.assertEqual(imported["queued"], 1)
        state = self.request("GET", "/api/state", origin=False)[2]
        self.assertEqual(state["links"][0]["relation"], "former_colleague_at")
        self.assertEqual(state["links"][0]["notes"], "Worked together")
        self.assertEqual(state["interactions"][0]["id"], interaction["interaction_id"])
        current = next(r for r in state["records"] if r["id"] == person["id"])
        self.assertEqual(current["last_touch"], "2026-09-20")
        self.assertEqual(state["review_queue"][0]["payload"]["observed_company"], "Former Co")
        self.assertNotIn("payload_json", state["review_queue"][0])

    def test_state_says_how_fresh_the_data_is(self):
        self.assertEqual(self.request("GET", "/api/state", origin=False)[2]["freshness"], {"last_success": ""})
        conn = db.connect(self.path)
        conn.execute("INSERT INTO scan_runs(run_id,date,receipt_json,status) VALUES(?,?,?,?)", (
            "ingest:2026-09-25T05:30:00Z", "2026-09-25",
            json.dumps({"started_at": "2026-09-25T05:30:00Z", "failures": ["feed:reed:London"],
                        "fetch": {"requests": 90, "recovered": 2, "failed": {"timeout": 1}}}), "partial"))
        conn.close()
        fresh = self.request("GET", "/api/state", origin=False)[2]["freshness"]
        self.assertEqual((fresh["ingest_at"], fresh["ingest_status"], fresh["failures"], fresh["fetch"]["recovered"]),
                         ("2026-09-25T05:30:00Z", "partial", ["feed:reed:London"], 2))

    def test_a_page_capped_feed_is_not_reported_as_a_failure(self):
        conn = db.connect(self.path)
        conn.execute("INSERT INTO source_health(scope,provider,state,observed_at,last_success,item_count,detail) VALUES(?,?,?,?,?,?,?)",
                     ("feed:reed:London", "reed", "partial", "2026-09-25T05:30:00Z", "2026-09-25T05:30:00Z", 100, "{}"))
        conn.execute("INSERT INTO scan_runs(run_id,date,receipt_json,status) VALUES(?,?,?,?)", (
            "ingest:2026-09-25T05:30:00Z", "2026-09-25",
            json.dumps({"started_at": "2026-09-25T05:30:00Z", "failures": ["feed:reed:London", "boards and feeds"]}), "partial"))
        conn.commit()
        conn.close()
        fresh = self.request("GET", "/api/state", origin=False)[2]["freshness"]
        self.assertEqual((fresh["failures"], fresh["capped"]), (["boards and feeds"], ["feed:reed:London"]))

    def test_state_declares_what_the_page_renders(self):
        """app.js draws lanes, bands and decision buttons only from what /api/state declares."""
        conn = db.connect(self.path)
        job = records.create(conn, dict(type="job", name="Test Lead", engagement="contract"))["record"]
        records.create(conn, dict(type="job", name="Engineering Manager", engagement="permanent"))
        records.create(conn, dict(type="tender", name="Testing services"))
        records.draft(conn, dict(record_id=job["id"], text="Please review this draft", revision=job["revision"]))
        records.create_decision(conn, dict(subject=job["id"], kind="draft", question="Approve?"))
        conn.close()
        state = self.request("GET", "/api/state", origin=False)[2]
        lanes = {r["name"]: r["relevance"]["lane"] for r in state["records"] if r["type"] in ("job", "tender")}
        self.assertEqual(lanes, {"Test Lead": "contract", "Engineering Manager": "permanent", "Testing services": "contract"})
        decision = state["decisions"][0]
        self.assertEqual((decision["record_id"], decision["record_name"], decision["status"]), (job["id"], "Test Lead", "open"))
        self.assertTrue(all(c["id"] and c["label"] and c["next"] for c in decision["choices"]))
        waiting = next(r for r in state["records"] if r["id"] == job["id"])
        self.assertEqual((waiting["owner"], waiting["working_state"]), ("owner", "waiting"))

    def test_answering_a_decision_over_http(self):
        conn = db.connect(self.path)
        agency = records.create(conn, dict(type="agency", name="YunoJuno"))["record"]
        did = records.create_decision(conn, dict(subject=agency["id"], kind="join", question="Join?"))["decision"]["id"]
        conn.close()
        state = lambda: next(d for d in self.request("GET", "/api/state", origin=False)[2]["decisions"] if d["id"] == did)
        d = state()
        answered = self.request("POST", f"/api/decisions/{did}", {"choice": "later", "note": "", "revision": d["revision"]})[2]["decision"]
        self.assertEqual((answered["status"], answered["answer"]), ("answered", "later"))
        # The page's "Change answer" sends the new revision; a second tab still holding the old one is refused.
        changed = self.request("POST", f"/api/decisions/{did}", {"choice": "help", "note": "Profile first", "revision": answered["revision"]})[2]["decision"]
        self.assertEqual((changed["answer"], changed["note"]), ("help", "Profile first"))
        status, error = self.rejected("POST", f"/api/decisions/{did}", {"choice": "no", "revision": d["revision"]})
        self.assertEqual(status, 409)
        self.assertIn("Reload", error["error"])
        self.assertEqual(self.rejected("POST", f"/api/decisions/{did}", {"choice": "approve", "revision": changed["revision"]})[0], 400)
        self.assertEqual(self.rejected("POST", f"/api/decisions/{did}", {"choice": "no", "revision": changed["revision"]}, origin=False)[0], 403)
        self.assertEqual(self.rejected("POST", "/api/decisions/99999", {"choice": "no", "revision": 1})[0], 400)
        # Once the agent has processed it, the page can no longer answer it.
        conn = db.connect(self.path)
        records.process_decision(conn, did, "done", "Profile text drafted.", revision=changed["revision"])
        conn.close()
        processed = state()
        self.assertEqual((processed["status"], processed["outcome"]), ("done", "Profile text drafted."))
        self.assertEqual(self.rejected("POST", f"/api/decisions/{did}", {"choice": "no", "revision": processed["revision"]})[0], 409)

    def test_review_accept_and_stale_review_conflict(self):
        csv_text = "First Name,Last Name,URL,Email Address,Company,Position,Connected On\nAlex,Example,https://www.linkedin.com/in/alex-example/,,Former Co,Engineer,01 Jan 2020\n"
        self.request("POST", "/api/import/linkedin", {"csv": csv_text})
        item = self.request("GET", "/api/state", origin=False)[2]["review_queue"][0]
        accepted = self.request("POST", f"/api/review/{item['id']}", {
            "resolution": "accept", "revision": item["revision"],
        })[2]
        self.assertEqual(accepted["record"]["name"], "Alex Example")
        code, _ = self.rejected("POST", f"/api/review/{item['id']}", {
            "resolution": "reject", "revision": item["revision"],
        })
        self.assertEqual(code, 409)


class RunStatusTests(ServerCase):
    def test_email_lead_read_is_safe_and_shortlist_requires_explicit_same_origin_action(self):
        from test_email_leads import item
        pending = self.path.parent / 'pending'; pending.mkdir()
        (pending / 'observations.json').write_text(json.dumps(dict(items={'fixture':item()}, sources={})))
        lead = self.request('GET', '/api/email-leads', origin=False)[2]['items'][0]
        self.assertNotIn('strong match', json.dumps(lead))
        url = '/api/email-leads/' + lead['id']
        self.assertEqual(self.request('GET', url, origin=False)[2]['assessment'], 'Unassessed')
        self.assertEqual(self.rejected('POST', url+'/shortlist', {'confirm':True}, origin=False)[0], 403)
        self.assertEqual(self.rejected('POST', url+'/shortlist', {})[0], 400)
        self.assertEqual(self.rejected('POST', url+'/shortlist', {'confirm':1})[0], 400)
        self.assertEqual(self.request('GET', '/api/state', origin=False)[2]['records'], [])
        saved = self.request('POST', url+'/shortlist', {'confirm':True})[2]['record']
        self.assertEqual(saved['status'], 'pursue')
        self.assertNotEqual(saved['availability']['status'], 'open')
        self.assertEqual(self.request('GET', '/api/email-leads', origin=False)[2]['items'], [])
        self.assertEqual(self.request('GET', url, origin=False)[2]['record_id'], saved['id'])

    """An open page re-reads run status alone, so a long-open tab does not judge an old run."""

    def add_run(self, kind, at):
        conn = db.connect(self.path)
        conn.execute('INSERT INTO scan_runs(run_id,date,status,receipt_json) VALUES(?,?,?,?)',
                     (f'{kind}:{at}', at[:10], 'ok', json.dumps(dict(started_at=at, finished_at=at, status='ok'))))
        conn.commit()
        conn.close()

    def test_status_returns_each_latest_run_without_building_the_page_state(self):
        self.add_run('email', '2026-10-28T12:15:25Z')
        first = self.request('GET', '/api/status', origin=False)[2]
        self.assertEqual(set(first), {'email_processing', 'research_processing', 'caretaker_processing'})
        self.assertEqual(first['email_processing']['last_attempt'], '2026-10-28T12:15:25Z')
        summary = self.request('GET', '/api/state?view=summary', origin=False)[2]
        self.assertEqual({key: summary[key] for key in first}, first)
        self.add_run('email', '2026-10-28T13:45:33Z')
        with unittest.mock.patch.object(workspace_state, 'state', side_effect=AssertionError('full state built')):
            later = self.request('GET', '/api/status', origin=False)[2]
        self.assertEqual(later['email_processing']['last_attempt'], '2026-10-28T13:45:33Z')


class DiagnosticsTests(ServerCase):
    """What the service tells an operator: the deployed commit, and one log line per save or refusal."""

    def test_health_names_the_deployed_revision(self):
        with unittest.mock.patch.dict(os.environ, {"WO_REVISION": "abc1234"}):
            self.assertEqual(self.request("GET", "/api/health", origin=False)[2]["revision"], "abc1234")
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WO_REVISION", None)
            self.assertEqual(self.request("GET", "/api/health", origin=False)[2]["revision"], "")

    def test_saves_and_refusals_are_logged_without_their_content(self):
        created = self.request("POST", "/api/records", {"type": "person", "name": "Secret Name"})[2]["record"]
        with self.assertLogs(level="INFO") as logs:
            self.request("PATCH", f"/api/records/{created['id']}", {"summary": "private text", "revision": created["revision"]})
            self.rejected("PATCH", f"/api/records/{created['id']}", {"summary": "again", "revision": created["revision"]})
            self.rejected("POST", "/api/records", {"type": "person", "name": "x"}, origin=False)
            self.request("GET", "/api/state", origin=False)
        text = "\n".join(logs.output)
        self.assertRegex(text, rf"PATCH /api/records/{created['id']} 200 \d+ms")
        self.assertRegex(text, rf"PATCH /api/records/{created['id']} 409 \d+ms")
        self.assertRegex(text, r"POST /api/records 403 \d+ms")
        self.assertNotIn("GET /api/state", text)  # reads that succeed are not logged
        self.assertNotIn("private text", text)
        self.assertNotIn("Secret Name", text)

    def test_page_loads_and_section_views_are_counted_without_ids(self):
        iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
        with self.assertLogs(level="INFO") as logs:
            self.request("GET", "/", origin=False, headers={"User-Agent": iphone})
            self.request("GET", "/", origin=False, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            self.request("POST", "/api/seen/week", {})
            self.request("POST", "/api/seen/people/record", {})
            code, _ = self.rejected("POST", "/api/seen/CP-001/record", {})
        text = "\n".join(logs.output)
        self.assertRegex(text, r"GET / 200 \d+ms iphone")
        self.assertRegex(text, r"GET / 200 \d+ms desktop")
        self.assertRegex(text, r"POST /api/seen/week 200 \d+ms")
        self.assertRegex(text, r"POST /api/seen/people/record 200 \d+ms")
        self.assertEqual(code, 404)


# The fields the browser's edit form sends back, unchanged, on Save.
EDIT_FORM = ("name", "organisation", "role", "summary", "location", "url", "email", "phone", "pay",
             "engagement", "workplace", "ir35", "date", "published_deadline", "evidence_urls",
             "verification", "known_context", "notes")


def contract_fixture(path):
    """A small store holding every review and state case the UI offers an action for."""
    conn = db.connect(path)
    db.migrate(conn)
    person = lambda name, **f: records.create(conn, dict(type="person", name=name, **f))["record"]
    person("Dana Existing", email="dana@agency.example")
    person("Sam Namesake", email="sam@one.example")
    gone = person("Ari Archived", email="ari@old.example")
    records.archive(conn, gone["id"], {"revision": gone["revision"], "reason": "test"})
    agency = records.create(conn, dict(type="agency", name="Agency Ltd"))["record"]

    def candidate(**payload):
        conn.execute("INSERT INTO review_queue(kind,payload_json,source,created_at) VALUES('person_candidate',?,?,?)",
                     (json.dumps(payload), "test", db.now()))

    candidate(name="Nia New", agency_id=agency["id"], email="nia@agency.example")  # no match
    candidate(name="Dana Existing", email="dana@agency.example")  # same email as a person
    candidate(name="Sam Namesake", email="sam@two.example")  # same name only
    candidate(name="Ari Archived", email="ari@old.example")  # matches only an archived person
    candidate(name="Twin Card")  # accepting this makes the next a name match
    candidate(name="Twin Card", email="twin2@x.example")
    records.queue_linkedin_person(conn, {"First Name": "Sam", "Last Name": "Namesake", "URL": "https://www.linkedin.com/in/sam-n",
                                         "Company": "Agency Ltd", "Position": "Recruiter"})
    records.queue_linkedin_person(conn, {"First Name": "Lee", "Last Name": "Linked", "URL": "https://www.linkedin.com/in/lee-l",
                                         "Company": "Other", "Position": "Engineer"})
    # One live decision of every declared kind, so every declared answer is sent below.
    job = records.create(conn, dict(type="job", name="Programme Test Lead", engagement="contract"))["record"]
    records.draft(conn, dict(record_id=job["id"],title="Draft",text="Hi Charlie,",revision=job["revision"]))
    subjects = {"join": agency["id"], "draft": job["id"], "approve": "workspace:local-acceptance",
                "failure": "workspace:source-board-lever-example"}
    assert set(subjects) == set(states.DECISION_KINDS), "add a fixture decision for every declared kind"
    for kind, subject in subjects.items():
        records.create_decision(conn, dict(subject=subject, kind=kind, question=f"{kind}?"))
    conn.close()


class ContractTests(ServerCase):
    def test_every_declared_outcome_action_is_accepted(self):
        # Synthetic records only: these actions report facts and must never be applied to a live copy.
        for typ in ('person','company','agency','job','opportunity','source','event','tender'):
            record=self.create(typ,f'Outcome test {typ}')
            row=next(r for r in self.state()['records'] if r['id']==record['id'])
            self.assertEqual(bool(row['outcome_actions']),typ in ('person','company','agency','job','opportunity'))
            for action in row['outcome_actions']:
                with self.subTest(type=typ,kind=action['kind']):
                    result=self.request('POST',f"/api/records/{record['id']}/outcome",dict(kind=action['kind'],revision=record['revision'],
                        **({'company': 'Example Client'} if action.get('asks') == 'company' else {})))[2]
                    record=result['record']
                    self.assertEqual(result['outcome']['kind'],action['kind'])

    """Every action the workspace offers must be one the API accepts.

    The UI renders review buttons and state changes from what `/api/state` declares, so these tests
    take each declared option and send it as the browser would. The separate check_contract.py
    entry point checks a selected development database on disposable migrated copies.
    """

    def setUp(self):
        super().setUp()
        self.fresh()

    def fresh(self):
        self.http.db = seeded_copy('contract', contract_fixture, Path(self.temp.name) / f"fixture-{os.urandom(4).hex()}.db")

    def state(self):
        return self.request("GET", "/api/state", origin=False)[2]

    def send(self, item, action, state):
        body = {k: action[k] for k in ("resolution", "target_id", "accept_as_new") if k in action}
        if action.get("pick_target"):
            body["target_id"] = next(r["id"] for r in state["records"] if r["type"] == "person")
        body["revision"] = item["revision"]
        return self.request("POST", f"/api/review/{item['id']}", body)

    def test_every_offered_review_action_succeeds(self):
        count = len(self.state()["review_queue"])
        self.assertGreaterEqual(count, 8)
        for index in range(count):
            self.fresh()
            for choice in range(len(self.state()["review_queue"][index]["actions"])):
                self.fresh()
                state = self.state()
                item = state["review_queue"][index]
                action = item["actions"][choice]
                with self.subTest(card=item["payload"].get("name"), action=action["label"]):
                    self.assertEqual(self.send(item, action, state)[0], 200)

    def test_matched_cards_offer_merge_and_never_a_refused_accept(self):
        cards = {(q["kind"], q["payload"]["name"], q["payload"].get("email", "")): q for q in self.state()["review_queue"]}
        labels = lambda key: [a["label"] for a in cards[key]["actions"]]
        self.assertEqual(labels(("person_candidate", "Dana Existing", "dana@agency.example")), ["Merge into Dana Existing", "Reject"])
        self.assertEqual(labels(("person_candidate", "Sam Namesake", "sam@two.example")), ["Merge into Sam Namesake", "Accept as new", "Reject"])
        self.assertEqual(labels(("person_candidate", "Ari Archived", "ari@old.example"))[0], "Accept")
        self.assertEqual(labels(("linkedin_person", "Lee Linked", ""))[0], "Accept")

    def test_accepting_cards_in_turn_keeps_working(self):
        """The owner's report: the top card accepted, then later accepts returned 400."""
        seen = 0
        while True:
            state = self.state()
            if not state["review_queue"]:
                break
            item = state["review_queue"][0]
            action = next(a for a in item["actions"] if a.get("primary"))
            with self.subTest(card=item["payload"].get("name"), action=action["label"]):
                self.assertEqual(self.send(item, action, state)[0], 200)
            seen += 1
            self.assertLess(seen, 5000)

    def test_repeat_submit_is_a_readable_conflict(self):
        state = self.state()
        if not state["review_queue"]:
            self.skipTest("no review cards")
        item = state["review_queue"][0]
        action = next(a for a in item["actions"] if a.get("primary"))
        self.assertEqual(self.send(item, action, state)[0], 200)
        with self.assertRaises(HTTPError) as caught:
            self.send(item, action, state)
        self.assertEqual(caught.exception.code, 409)
        self.assertIn("Reload", json.load(caught.exception)["error"])
        caught.exception.close()

    def test_every_declared_decision_answer_is_accepted(self):
        state = self.state()
        self.assertEqual({d["kind"] for d in state["decisions"]}, set(states.DECISION_KINDS))
        for index, decision in enumerate(state["decisions"]):
            for choice in decision["choices"]:
                self.fresh()
                d = self.state()["decisions"][index]
                with self.subTest(kind=d["kind"], choice=choice["id"]):
                    body = {"choice": choice["id"], "revision": d["revision"], "note": "Because" if choice["note"] else ""}
                    status, _, result = self.request("POST", f"/api/decisions/{d['id']}", body)
                    self.assertEqual((status, result["decision"]["status"]), (200, "answered"))

    def test_a_required_note_is_a_readable_refusal(self):
        d = next(x for x in self.state()['decisions'] if x['kind']!='draft' and any(c['note'] for c in x['choices']))
        choice=next(c for c in d['choices'] if c['note'])
        with self.assertRaises(HTTPError) as caught:
            self.request('POST', f"/api/decisions/{d['id']}", dict(choice=choice['id'],revision=d['revision']))
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("Add a note", json.load(caught.exception)["error"])
        caught.exception.close()

    def test_every_declared_transition_is_accepted(self):
        types = {"opportunity": "opportunity", "org": "agency", "job": "job", "person": "person"}
        for (entity, field), table in states.TRANSITIONS.items():
            for start in [None, *table]:
                probe = self.create(types[entity], f"{entity} {field} {start}", **({field: start} if start else {}))
                offered = next(r for r in self.state()["records"] if r["id"] == probe["id"])["transitions"].get(field, [])
                self.assertEqual(offered, list(states.allowed_from(entity, field, start)))
                for to in offered:
                    record = self.create(types[entity], f"{entity} {field} {start}", **({field: start} if start else {}))
                    with self.subTest(entity=entity, field=field, start=start, to=to):
                        self.assertEqual(self.request("POST", f"/api/records/{record['id']}/transition", {
                            "revision": record["revision"], "field": field, "to": to})[0], 200)

    def test_every_declared_person_choice_is_accepted_and_nothing_else(self):
        vocab = self.state()["vocabularies"]["person"]
        person, company = self.create("person", "Choice probe"), self.create("company", "Choice company")
        for field in records.PERSON_CHOICES:
            for value in [*vocab[field], None]:
                with self.subTest(field=field, value=value):
                    current = next(r for r in self.state()["records"] if r["id"] == person["id"])
                    saved = self.request("PATCH", f"/api/records/{person['id']}", {field: value, "revision": current["revision"]})
                    self.assertEqual((saved[0], saved[2]["record"][field]), (200, value))
            current = next(r for r in self.state()["records"] if r["id"] == person["id"])
            self.assertEqual(self.rejected("PATCH", f"/api/records/{person['id']}", {field: "carrier pigeon", "revision": current["revision"]})[0], 400)
            self.assertEqual(self.rejected("PATCH", f"/api/records/{company['id']}", {field: vocab[field][0], "revision": company["revision"]})[0], 400)

    def test_resending_a_link_and_creating_each_type_succeed(self):
        person, company = self.create("person", "Link probe"), self.create("company", "Link target")
        for _ in range(2):
            current = next(r for r in self.state()["records"] if r["id"] == person["id"])
            self.assertEqual(self.request("POST", "/api/links", {
                "from_id": person["id"], "to_id": company["id"], "relation": "works_at", "revision": current["revision"]})[0], 200)
        for typ in records.TYPES:
            with self.subTest(type=typ):
                self.assertEqual(self.request("POST", "/api/records", {"type": typ, "name": f"Probe {typ}"})[0], 200)

    def test_saving_an_unchanged_record_succeeds(self):
        for record in self.state()["records"]:
            with self.subTest(record=record["id"]):
                body = {k: record.get(k) or "" for k in EDIT_FORM}
                self.assertEqual(self.request("PATCH", f"/api/records/{record['id']}", dict(body, revision=record["revision"]))[0], 200)



if __name__ == "__main__":
    unittest.main()

class ProgressContractTests(ServerCase):
    """Every action an approach's progress (or the week) declares is accepted by the API when sent
    exactly as the browser sends it. Uses the synthetic development fixture (tools/make_fixture.py)."""

    def fresh(self):
        import importlib, sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
        make_fixture = importlib.import_module('make_fixture')
        self.http.db = seeded_copy('development', make_fixture.build, Path(self.temp.name) / f"progress-{os.urandom(4).hex()}.db")

    def state(self):
        return self.request("GET", "/api/state", origin=False)[2]

    def send(self, action, state):
        records = {r['id']: r for r in state['records']}
        rid = action.get('record_id')
        revision = records[rid]['revision'] if rid in records else None
        method = action['method']
        if method == 'next_step':
            return self.request('POST', f"/api/records/{rid}/next-step", dict(revision=revision, action_id=action['action_id'],
                                next_step={'kind':'mine', 'text':'Send the agreed example'}))
        if method == 'outcome':
            return self.request('POST', f"/api/records/{rid}/outcome", dict(kind=action['kind'], revision=revision, action_id=action['action_id'],
                                **({'channel': action['channel']} if action.get('channel') else {}),
                                **({'company': 'Contract Test Co'} if action.get('asks') == 'company' else {})))
        if method == 'transition':
            return self.request('POST', f"/api/records/{rid}/transition", dict(field=action['field'], to=action['to'], revision=revision))
        if method == 'handover':
            return self.request('POST', f"/api/records/{rid}/handover", dict(revision=revision, action_id=action['action_id'],
                                company_id=action.get('company_id') or '', next_step='Agree a scoped proposal'))
        if method == 'correct':
            body = dict(revision=revision, note='Contract test')
            body.update({k: action[k] for k in ('outcome_id', 'field') if k in action})
            return self.request('POST', f"/api/records/{rid}/correct", body)
        if method == 'week':
            return self.request('POST', '/api/week/defer', dict(action_id=action['action_id']))
        if method == 'week_restore':
            return self.request('POST', '/api/week/restore', dict(action_id=action['action_id']))
        return None

    def declared(self, state):
        # Record ids are random per fixture build; names, kinds and titles are not.
        intel = state['intelligence']
        names = {r['id']: r['name'] for r in state['records']}
        found = []
        for item in intel['actions'] + intel['research']:
            key = (item['kind'], names.get(item['record_id']), item.get('company') or '', item.get('title'))
            for n, action in enumerate(item['progress']['actions']):
                found.append((key, n, action))
        # The fixture's random ids can break ties differently, so a week position may hold another
        # person between builds; whoever is there, their declared action must be accepted.
        for n, entry in enumerate(intel['week']['items']):
            for action in entry['actions']:
                found.append((('week', n), 0, action))
        for n, entry in enumerate(intel['week']['deferred']):
            for action in entry['actions']:
                found.append((('set aside', n), 0, action))
        return found

    def test_retired_writes_preserve_historical_versions_and_decisions(self):
        self.fresh()
        state=self.state()
        person=next(r for r in state['records'] if r['name']=='Alex Example')
        before=self.state()
        self.assertEqual(self.rejected('POST',f"/api/records/{person['id']}/draft",dict(
            revision=person['revision'],text='New message',purpose='message'))[0],410)
        for decision in state['decisions']:
            if decision['kind']=='draft':
                self.assertEqual(decision['choices'],[])
                self.assertTrue(decision['read_only'])
                self.assertEqual(self.rejected('POST',f"/api/decisions/{decision['id']}",dict(
                    choice='approve',revision=decision['revision']))[0],410)
        after=self.state()
        for key in ('drafts','decisions','outcomes','records'):
            self.assertEqual(before[key],after[key],key)

    def test_fixture_declares_retained_steps_and_no_retired_writes(self):
        self.fresh()
        methods={a['method'] for _,_,a in self.declared(self.state())}
        self.assertTrue({'outcome','transition','handover','correct'}<=methods,methods)
        self.assertFalse(methods & {'week','week_restore','copy','decision'})

    def test_old_week_links_cannot_change_the_preserved_week_history(self):
        self.fresh()
        before=self.state()['intelligence']['week']
        for endpoint in ('store','defer','restore'):
            self.assertEqual(self.rejected('POST','/api/week/'+endpoint,dict(action_id='historical'))[0],410)
        self.assertEqual(self.state()['intelligence']['week'],before)

    def test_an_opportunity_already_in_work_never_offers_a_handover(self):
        self.fresh()
        state = self.state()
        opp = next(i for i in state['intelligence']['actions'] if i['kind'] == 'opportunity')
        for kind in ('sent', 'replied', 'conversation', 'proposal'):
            revision = next(r['revision'] for r in state['records'] if r['id'] == opp['record_id'])
            status = self.request('POST', f"/api/records/{opp['record_id']}/outcome",
                                  dict(kind=kind, revision=revision, action_id=opp['id'], note=f'Contract test: {kind}'))[0]
            self.assertEqual(status, 200, kind)
            state = self.state()
            item = next(i for i in state['intelligence']['actions'] if i['id'] == opp['id'])
            self.assertNotIn('handover', [a['id'] for a in item['progress']['actions']], kind)
            if kind != 'sent':  # once they have acted, his note is the next step
                self.assertTrue(item['progress']['next'].startswith(f'Contract test: {kind}. '), item['progress']['next'])

    def test_every_declared_progress_action_is_accepted(self):
        self.fresh()
        cases = [(item_id, n, a['method']) for item_id, n, a in self.declared(self.state())
                 if a['method'] in ('outcome', 'transition', 'handover', 'correct', 'week', 'week_restore')]
        self.assertGreater(len(cases), 10)
        for item_id, n, method in cases:
            self.fresh()
            state = self.state()
            match = [a for i, k, a in self.declared(state) if i == item_id and k == n]
            with self.subTest(item=item_id, action=n, method=method):
                self.assertTrue(match, 'the declared action changed between identical fixtures')
                status = self.send(match[0], state)[0]
                self.assertEqual(status, 200)


class IncomingRoleAPITests(ServerCase):
    def test_private_intake_preview_save_replay_and_document_download(self):
        import base64
        payload=dict(request_id='api-incoming',name='Fixture Engineer',origin='recruiter',origin_date='2026-10-15',
                     stage='screening_completed',stage_date='2026-10-21',source_ref='Fixture email',
                     documents=[dict(filename='private.txt',kind='preparation',content=base64.b64encode(b'Proposed talking points').decode())])
        preview=self.request('POST','/api/incoming/preview',payload)[2]
        self.assertIsNone(preview['match'])
        saved=self.request('POST','/api/incoming/save',payload)[2]
        self.assertEqual(saved['record']['status'],'interview')
        again=self.request('POST','/api/incoming/save',payload)[2]
        self.assertTrue(again['replayed'])
        doc=saved['record']['incoming']['documents'][0]
        response=urlopen(self.base+'/api/documents/'+doc['id'])
        self.assertIn('attachment',response.headers['Content-Disposition'])
        self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
        self.assertEqual(response.read(),b'Proposed talking points')
        self.assertNotIn('content_base64',json.dumps(saved))
    def test_intake_rejects_cross_origin_and_bad_stage(self):
        self.assertEqual(self.rejected('POST','/api/incoming/save',{},origin=False)[0],403)
        self.assertEqual(self.rejected('POST','/api/incoming/save',dict(name='Fixture',stage='invented'))[0],400)
