"""Private mail receipt boundaries, exercised using synthetic MIME and local HTTP only."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import http.client
import io
import json
from pathlib import Path
import selectors
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mail_intake as mi

RAW = b"From: alerts@example.test\r\nSubject: Synthetic test\r\n\r\nSynthetic mail only."
META = dict(envelope_from="sender@example.test", envelope_to="alerts@example.com",
            received_at="2026-10-07T10:11:12.123Z")
TOKEN = "synthetic-test-token-never-real-0123456789"


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "private" / "mail.db"

    def test_receipt_follows_durable_commit_and_duplicate_preserves_original(self):
        spool = mi.MailSpool(self.path)
        first = spool.receive(RAW, **META)
        self.assertEqual(first, dict(receipt_id=hashlib.sha256(RAW).hexdigest(), sequence=1,
                                     received_at=META["received_at"], duplicate=False))
        reader = mi.MailSpool(self.path, readonly=True)
        self.assertEqual(reader.list_messages(), [dict(sequence=1, receipt_id=first["receipt_id"], raw=RAW, **META)])
        again = spool.receive(RAW, **(META | {"received_at": "2026-10-08T01:00:00Z"}))
        self.assertEqual(again, first | {"duplicate": True})
        self.assertEqual(reader.info(), dict(store_id=spool.info()["store_id"], count=1, bytes=len(RAW), max_sequence=1))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_readonly_missing_database_does_not_create_anything(self):
        with self.assertRaises(mi.SpoolUnavailableError):
            mi.MailSpool(self.path, readonly=True)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_new_ancestor_entries_are_synced_before_initialization_succeeds(self):
        path = self.path.parent / "nested" / "mail.db"
        with patch.object(mi, "_sync_directory", wraps=mi._sync_directory) as sync:
            spool = mi.MailSpool(path)
        self.assertEqual([call.args[0] for call in sync.call_args_list],
                         [Path(self.tmp.name), self.path.parent, path.parent])
        self.assertEqual(spool.info()["count"], 0)
        failed = Path(self.tmp.name) / "failed" / "mail.db"
        with patch.object(mi, "_sync_directory", side_effect=OSError("private failure")):
            with self.assertRaises(mi.SpoolUnavailableError):
                mi.MailSpool(failed)
        self.assertFalse(failed.exists())

    def test_readonly_receive_and_wrong_database_are_rejected(self):
        mi.MailSpool(self.path)
        reader = mi.MailSpool(self.path, readonly=True)
        with self.assertRaises(mi.SpoolUnavailableError):
            reader.receive(RAW, **META)
        other = Path(self.tmp.name) / "other.db"
        with sqlite3.connect(other) as conn:
            conn.execute("CREATE TABLE unrelated(value)")
        before = other.read_bytes()
        with self.assertRaises(mi.SpoolUnavailableError):
            mi.MailSpool(other)
        self.assertEqual(other.read_bytes(), before)

    def test_capacity_is_atomic_and_duplicates_work_when_full(self):
        spool = mi.MailSpool(self.path, max_bytes=len(RAW), max_messages=1)
        spool.receive(RAW, **META)
        self.assertTrue(spool.receive(RAW, **META)["duplicate"])
        with self.assertRaises(mi.SpoolCapacityError):
            spool.receive(RAW + b"second", **META)
        self.assertEqual(spool.info()["count"], 1)
        with self.assertRaises(mi.SpoolCapacityError):
            mi.MailSpool(Path(self.tmp.name) / "bytes.db", max_bytes=len(RAW) - 1).receive(RAW, **META)

    def test_invalid_mime_envelope_timestamp_and_sizes_do_not_create_receipts(self):
        spool = mi.MailSpool(self.path)
        cases = [(b"", META), ("not bytes", META), (b"not MIME", META), (b"Bad Header\r\n\r\nbody", META),
                 (RAW + b"x" * mi.MAX_MESSAGE_BYTES, META),
                 (RAW, META | {"envelope_to": "other@example.test"}),
                 (RAW, META | {"envelope_from": "Name <sender@example.test>"}),
                 (RAW, META | {"envelope_from": "sender@example.test\r\nInjected: no"}),
                 (RAW, META | {"received_at": "today"}),
                 (RAW, META | {"received_at": "2026-10-07T12:00:00+01:00"})]
        for raw, meta in cases:
            with self.subTest(meta=meta, size=len(raw)), self.assertRaises(mi.InvalidMailError):
                spool.receive(raw, **meta)
        self.assertEqual(spool.info()["count"], 0)

    def test_concurrent_receives_deduplicate_and_do_not_overrun_capacity(self):
        spool = mi.MailSpool(self.path, max_messages=3)
        def receive(i):
            try:
                return spool.receive(RAW + str(i % 8).encode(), **META)
            except mi.SpoolCapacityError:
                return None
        with ThreadPoolExecutor(max_workers=12) as pool:
            receipts = list(pool.map(receive, range(40)))
        info = spool.info()
        self.assertEqual(info["count"], 3)
        self.assertEqual(len({r["receipt_id"] for r in receipts if r}), 3)
        self.assertEqual(len([r for r in receipts if r and not r["duplicate"]]), 3)
        self.assertEqual(len({r["sequence"] for r in receipts if r}), 3)

    def test_storage_failure_is_safe_and_no_receipt_is_returned(self):
        spool = mi.MailSpool(self.path)
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TRIGGER fail_receive BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT, 'private raw token'); END")
        with self.assertRaises(mi.SpoolUnavailableError) as error:
            spool.receive(RAW, **META)
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(spool.info()["count"], 0)

    def test_commit_failure_rolls_back_insertion_and_retry_can_recover(self):
        spool = mi.MailSpool(self.path)
        original = spool._connection
        class FailedCommit:
            def __init__(self, connection):
                self.connection = connection
            def execute(self, *args):
                return self.connection.execute(*args)
            def commit(self):
                raise sqlite3.OperationalError("private disk failure")
        @contextlib.contextmanager
        def connection(*, write=False):
            with original(write=write) as conn:
                yield FailedCommit(conn) if write else conn
        with patch.object(spool, "_connection", connection):
            with self.assertRaises(mi.SpoolUnavailableError) as error:
                spool.receive(RAW, **META)
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(spool.info()["count"], 0)
        self.assertFalse(spool.receive(RAW, **META)["duplicate"])

    def test_sequence_reader_is_bounded_and_stable_across_reopen(self):
        spool = mi.MailSpool(self.path)
        original = spool.info()["store_id"]
        for i in range(4):
            spool.receive(RAW + str(i).encode(), **META)
        reader = mi.MailSpool(self.path, readonly=True)
        self.assertEqual(reader.info()["store_id"], original)
        self.assertEqual([m["sequence"] for m in reader.list_messages(after=1, limit=2)], [2, 3])
        self.assertEqual(reader.list_messages(after=4), [])
        for after, limit in [(-1, 1), (True, 1), (0, 0), (0, 201)]:
            with self.assertRaises(mi.InvalidMailError):
                reader.list_messages(after=after, limit=limit)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spool = mi.MailSpool(Path(self.tmp.name) / "mail.db")
        self.server = mi.make_server(self.spool, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.port = self.server.server_address[1]

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def headers(self):
        return {"Authorization": "Bearer " + TOKEN, "Content-Type": "message/rfc822",
                "X-Alert-Envelope-From": META["envelope_from"], "X-Alert-Envelope-To": META["envelope_to"],
                "X-Alert-Received-At": META["received_at"]}

    def request(self, method="POST", path="/v1/email", body=RAW, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        self.addCleanup(conn.close)
        conn.request(method, path, body, self.headers() if headers is None else headers)
        response = conn.getresponse()
        data = response.read()
        self.assertLess(len(data), 4096)
        self.assertNotIn(TOKEN.encode(), data)
        self.assertNotIn(RAW, data)
        return response.status, json.loads(data)

    def test_authenticated_delivery_and_duplicate_return_only_safe_receipt(self):
        first = self.request()
        self.assertEqual(first[0], 201)
        self.assertEqual(set(first[1]), {"receipt_id", "sequence", "received_at", "duplicate"})
        second = self.request()
        self.assertEqual(second, (200, first[1] | {"duplicate": True}))

    def test_auth_and_minimal_health_without_inbox_read_api(self):
        for auth in ["", "Bearer wrong", "Basic " + TOKEN]:
            self.assertEqual(self.request(headers=self.headers() | {"Authorization": auth})[0], 401)
        self.assertEqual(self.request("GET", "/health", body=None, headers={}), (200, {"status": "ready"}))
        self.assertEqual(self.request("GET", "/v1/messages", body=None, headers={})[0], 404)
        self.assertEqual(self.spool.info()["count"], 0)

    def test_health_reports_full_and_existing_duplicates_still_work(self):
        self.spool.max_messages = 1
        self.assertEqual(self.request()[0], 201)
        self.assertEqual(self.request("GET", "/health", body=None, headers={}), (503, {"status": "full"}))
        self.assertEqual(self.request()[0], 200)

    def test_mime_metadata_and_oversize_requests_fail_without_storage(self):
        for headers in [self.headers() | {"Content-Type": "text/plain"},
                        self.headers() | {"X-Alert-Envelope-To": "elsewhere@example.test"},
                        self.headers() | {"X-Alert-Received-At": "invalid"}]:
            self.assertIn(self.request(headers=headers)[0], [400, 415])
        self.assertEqual(self.request(headers=self.headers() | {"Content-Length": str(mi.MAX_MESSAGE_BYTES + 1)})[0], 413)
        self.assertEqual(self.spool.info()["count"], 0)

    def test_duplicate_length_auth_and_chunked_are_rejected(self):
        for extra in ["Content-Length: 1\r\n", "Authorization: Bearer other\r\n", "Transfer-Encoding: chunked\r\n",
                      "X-Alert-Envelope-To: other@example.test\r\n"]:
            headers = self.headers() | {"Content-Length": str(len(RAW))}
            wire = ("POST /v1/email HTTP/1.1\r\nHost: localhost\r\n" +
                    "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + extra + "\r\n").encode() + RAW
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
                conn.sendall(wire)
                response = conn.recv(4096)
            self.assertNotIn(b" 201 ", response)
            self.assertNotIn(b" 200 ", response)
        self.assertEqual(self.spool.info()["count"], 0)

    def test_truncated_body_has_no_receipt(self):
        headers = self.headers() | {"Content-Length": str(len(RAW) + 20)}
        wire = ("POST /v1/email HTTP/1.1\r\nHost: localhost\r\n" +
                "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n").encode() + RAW
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
            conn.sendall(wire)
            conn.shutdown(socket.SHUT_WR)
            response = conn.recv(4096)
        self.assertIn(b" 400 ", response)
        self.assertEqual(self.spool.info()["count"], 0)

    def test_lost_http_response_retries_the_original_committed_receipt(self):
        committed = threading.Event()
        receive = self.spool.receive
        def observed_receive(*args, **kwargs):
            receipt = receive(*args, **kwargs)
            committed.set()
            return receipt
        headers = self.headers() | {"Content-Length": str(len(RAW))}
        wire = ("POST /v1/email HTTP/1.1\r\nHost: localhost\r\n" +
                "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n").encode() + RAW
        with patch.object(self.spool, "receive", observed_receive):
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
                conn.sendall(wire)
                self.assertTrue(committed.wait(2))
                # Deliberately discard the response, as when a gateway times out after commit.
        status, receipt = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(receipt["duplicate"])
        self.assertEqual((receipt["sequence"], self.spool.info()["count"]), (1, 1))

    def test_capacity_and_storage_failure_response_do_not_log_private_material(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            with patch.object(self.spool, "receive", side_effect=mi.SpoolCapacityError("private-token")):
                self.assertEqual(self.request(), (507, {"error": "spool_full"}))
            with patch.object(self.spool, "receive", side_effect=RuntimeError("private-raw-token")):
                self.assertEqual(self.request(), (503, {"error": "storage_unavailable"}))
        self.assertEqual(output.getvalue(), "")


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.command = [sys.executable, "-B", str(Path(mi.__file__).resolve())]

    def test_help_runs_without_creating_a_spool(self):
        result = subprocess.run(self.command + ["--help"], cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--token-file", result.stdout)
        self.assertIn("--spool", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_token_exits_safely_without_a_traceback_or_private_path(self):
        token_path = self.root / "private-credential-filename"
        result = subprocess.run(self.command + ["--spool", str(self.root / "mail.db"), "--port", "0",
                                                "--token-file", str(token_path)],
                                cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "mail intake could not start; check private spool and token configuration\n")
        self.assertNotIn(str(token_path), result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_private_token_file_starts_loopback_receiver_and_shutdown_is_clean(self):
        token_path = self.root / "receiver-token"
        token_path.write_text(TOKEN + "\n")
        token_path.chmod(0o600)
        spool_path = self.root / "mail.db"
        process = subprocess.Popen(self.command + ["--spool", str(spool_path), "--port", "0", "--token-file", str(token_path)],
                                   cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def stop():
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)
            process.stdout.close()
            process.stderr.close()
        self.addCleanup(stop)
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(ready.select(timeout=5), "CLI did not emit its readiness line")
            line = process.stdout.readline()
        self.assertRegex(line, r"^mail intake listening on 127\.0\.0\.1:[0-9]+\n$")
        port = int(line.rstrip().rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        self.addCleanup(conn.close)
        conn.request("GET", "/health")
        health = conn.getresponse()
        self.assertEqual((health.status, json.loads(health.read())), (200, {"status": "ready"}))
        conn.request("POST", "/v1/email", RAW, {"Authorization": "Bearer " + TOKEN,
                     "Content-Type": "message/rfc822", "X-Alert-Envelope-From": META["envelope_from"],
                     "X-Alert-Envelope-To": META["envelope_to"], "X-Alert-Received-At": META["received_at"]})
        response = conn.getresponse()
        self.assertEqual(response.status, 201)
        self.assertEqual(json.loads(response.read())["receipt_id"], hashlib.sha256(RAW).hexdigest())
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))
        self.assertEqual(mi.MailSpool(spool_path, readonly=True).list_messages()[0]["raw"], RAW)


if __name__ == "__main__":
    unittest.main()
