"""Focused tests for provider HTML plain-text normalization, and the retry, classification and log
rules every fetch shares."""
from email.message import Message
import io
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import urllib.response

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import web_read  # noqa: E402
from web_read import strip_html  # noqa: E402


class StripHtmlTests(unittest.TestCase):
    def test_literal_html_uses_block_boundaries_and_decodes_entities(self):
        source = "<h2>Role &amp; team</h2><p>Build <strong>useful</strong> tools.</p><ul><li>Remote</li><li>£500/day</li></ul>"
        self.assertEqual(strip_html(source), "Role & team Build useful tools. Remote £500/day")

    def test_encoded_and_double_encoded_whole_documents_are_normalized(self):
        encoded = "&lt;p&gt;First&amp;nbsp;line&lt;br&gt;Second&lt;/p&gt;"
        double_encoded = "&amp;lt;div&amp;gt;A &amp;amp;amp; B&amp;lt;/div&amp;gt;"
        self.assertEqual(strip_html(encoded), "First line Second")
        self.assertEqual(strip_html(double_encoded), "A & B")

    def test_script_and_style_contents_are_not_exposed(self):
        source = "&lt;p&gt;Visible&lt;/p&gt;&lt;script&gt;steal()&lt;/script&gt;<style>.secret{}</style> End"
        self.assertEqual(strip_html(source), "Visible End")

    def test_plain_text_and_natural_angle_brackets_survive(self):
        self.assertEqual(strip_html("Use 5 < 10 and 12 > 9; AT&amp;T"), "Use 5 < 10 and 12 > 9; AT&T")

    def test_none_and_whitespace_are_safe(self):
        self.assertEqual(strip_html(None), "")
        self.assertEqual(strip_html("  plain\n\ttext  "), "plain text")


class _Response(io.BytesIO):
    def __init__(self, body=b"{}", status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for k, v in (headers or {"Content-Type": "application/json; charset=utf-8"}).items():
            self.headers[k] = v

    def geturl(self):
        return "https://example.test/final"


def _http(code, headers=None):
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://example.test/x", code, "status", msg, None)


class FetchSecurityTests(unittest.TestCase):
    def setUp(self):
        policy = patch.object(web_read, 'POLICY', web_read.RetryPolicy(sleep=lambda _: None))
        policy.start()
        self.addCleanup(policy.stop)

    def redirect(self, start, destination, headers=None, status=302):
        """Exercise the real urllib redirect chain with synthetic transport, never sockets."""
        calls = []
        destinations = destination if isinstance(destination, list) else [destination]

        class RedirectBody(io.BytesIO):
            def read(self, *args):
                raise AssertionError('Redirect bodies must be closed without an unbounded drain')

        def transport(handler, req):
            calls.append((req.full_url, dict(req.header_items())))
            response_headers = Message()
            response_headers['Content-Type'] = 'application/json'
            redirecting = len(calls) <= len(destinations)
            code = status if redirecting else 200
            if redirecting:
                response_headers['Location'] = destinations[len(calls) - 1]
            body = RedirectBody(b'ignored') if redirecting else io.BytesIO(b'{}')
            response = urllib.response.addinfourl(body, response_headers, req.full_url, code)
            response.msg = 'Synthetic response'
            return response

        with patch.object(urllib.request.HTTPSHandler, 'https_open', transport), \
                patch.object(urllib.request.HTTPHandler, 'http_open', transport):
            try:
                result = web_read.request(start, headers=headers, logged=False)
            except (ValueError, urllib.error.HTTPError) as error:
                return calls, error
        return calls, result

    def test_credentials_never_reach_a_different_origin(self):
        for headers in ({'Authorization': 'Basic synthetic-key'}, {'Cookie': 'session=synthetic'},
                        {'X-Api-Key': 'synthetic'}, {'X-Custom-Secret': 'synthetic'}):
            with self.subTest(headers=list(headers)):
                calls, result = self.redirect('https://provider.test/api', 'https://other.test/collect', headers)
                self.assertEqual(len(calls), 1, 'refuse before contacting the redirect destination')
                self.assertIsInstance(result, ValueError)
                self.assertNotIn('synthetic', str(result))

    def test_query_credentials_and_changed_ports_cannot_cross_origins(self):
        for start, destination in (
            ('https://provider.test/api?app_key=synthetic', 'https://other.test/collect'),
            ('https://provider.test/api?opaque=synthetic', 'https://provider.test:444/api'),
            ('https://provider.test/api?opaque=synthetic', 'https://provider.test:0/api'),
        ):
            calls, result = self.redirect(start, destination)
            self.assertEqual(len(calls), 1)
            self.assertIsInstance(result, ValueError)

    def test_same_origin_auth_and_public_cross_origin_redirects_still_work(self):
        calls, result = self.redirect('https://provider.test/api', 'https://provider.test:443/final',
                                      {'Authorization': 'Basic synthetic-key'})
        self.assertEqual(result['body'], b'{}')
        self.assertEqual(calls[1][1]['Authorization'], 'Basic synthetic-key')
        calls, result = self.redirect('http://public.test/page', 'https://other.test/page')
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['body'], b'{}')

    def test_https_downgrade_and_non_web_redirects_are_refused(self):
        for destination in ('http://provider.test/page', 'ftp://other.test/page',
                            'https://user:synthetic@other.test/page'):
            calls, result = self.redirect('https://provider.test/page', destination)
            self.assertEqual(len(calls), 1)
            self.assertIsInstance(result, ValueError)

    def test_a_redirect_to_a_private_or_loopback_address_is_refused(self):
        for destination in ('https://127.0.0.1/collect', 'https://169.254.169.254/latest',
                            'https://[::1]/x', 'https://100.64.0.10/x'):
            with self.subTest(destination=destination):
                calls, result = self.redirect('https://provider.test/page', destination)
                self.assertEqual(len(calls), 1, 'refuse before contacting the private destination')
                self.assertIsInstance(result, ValueError)

    def test_every_redirect_status_obeys_policy_and_retains_safe_relative_redirects(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                calls, result = self.redirect('https://provider.test/api', '/final', status=status)
                self.assertEqual(result['body'], b'{}')
                self.assertEqual(calls[-1][0], 'https://provider.test/final')
                calls, result = self.redirect('https://provider.test/api', 'http://provider.test/final', status=status)
                self.assertEqual(len(calls), 1)
                self.assertIsInstance(result, ValueError)

    def test_private_query_policy_survives_an_intermediate_redirect(self):
        calls, result = self.redirect('https://provider.test/api?key=synthetic',
                                      ['/next', 'https://other.test/collect'])
        self.assertEqual(len(calls), 2)
        self.assertIsInstance(result, ValueError)

    def test_redirect_loops_remain_bounded(self):
        calls, result = self.redirect('https://provider.test/api', ['/api'] * 20)
        self.assertLess(len(calls), 10)
        self.assertIsInstance(result, urllib.error.HTTPError)

    def test_response_size_is_bounded_even_without_honest_content_length(self):
        for headers in ({}, {'Content-Length': '1'}, {'Content-Length': '999999999'}):
            response = _Response(b'x' * 33, headers=headers)
            with patch.object(web_read, 'MAX_RESPONSE_BYTES', 32), self.assertRaises(ValueError):
                web_read.request('https://provider.test/api', opener=lambda *a, **k: response, logged=False)
            self.assertTrue(response.closed)

    def test_exact_response_limit_is_accepted(self):
        response = _Response(b'x' * 32)
        with patch.object(web_read, 'MAX_RESPONSE_BYTES', 32):
            result = web_read.request('https://provider.test/api', opener=lambda *a, **k: response, logged=False)
        self.assertEqual(result['body'], b'x' * 32)

    def test_slow_response_is_closed_and_not_retried(self):
        clock = [0]

        class SlowResponse(_Response):
            def read1(self, size):
                clock[0] += 31
                return b'x'

        response = SlowResponse()
        calls = []

        def opener(*args, **kwargs):
            calls.append(True)
            return response

        with patch.object(web_read.time, 'monotonic', lambda: clock[0]), \
                self.assertRaisesRegex(ValueError, 'time budget'):
            web_read.request('https://provider.test/api', opener=opener, logged=False)
        self.assertEqual(len(calls), 1)
        self.assertTrue(response.closed)

    def test_invalid_initial_url_never_opens_transport_or_echoes_credentials(self):
        for url in ('https://user:synthetic@provider.test/api', 'https://provider.test:bad/api?key=synthetic'):
            with patch.object(urllib.request, 'build_opener') as opener, self.assertRaises(ValueError) as caught:
                web_read.request(url)
            opener.assert_not_called()
            self.assertNotIn('synthetic', str(caught.exception))


class FetchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.slept = []
        self.now = [0.0]
        web_read.POLICY = web_read.RetryPolicy(clock=lambda: self.now[0], sleep=self.slept.append)
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "logs" / "fetch.jsonl"
        env = patch.dict(os.environ, {"WO_FETCH_LOG": str(self.log), "WO_FETCH_RUN": "ingest:test"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)

    def opener(self, *outcomes):
        calls = []

        def open_(req, timeout):
            calls.append(req.full_url)
            out = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(out, BaseException):
                raise out
            return out
        open_.calls = calls
        return open_

    def test_transient_failures_are_retried_then_recover_and_log_one_line(self):
        opener = self.opener(_http(503), socket.timeout("timed out"), _Response(b'{"ok": 1}'))
        result = web_read.request("https://example.test/api?app_key=SECRET", opener=opener)
        self.assertEqual(json.loads(result["body"]), {"ok": 1})
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(self.slept, [2.0, 6.0])
        [line] = web_read.read_log(self.log)
        self.assertEqual((line["ok"], line["attempts"], line["run"]), (True, 3, "ingest:test"))
        self.assertNotIn("SECRET", self.log.read_text())
        self.assertEqual(line["url"], "https://example.test/api")

    def test_challenge_refusal_and_missing_page_are_never_retried(self):
        for error, kind in ((_http(403, {"cf-mitigated": "challenge"}), "challenged"), (_http(403), "refused"),
                            (_http(404), "not-found")):
            opener = self.opener(error, _Response())
            with self.assertRaises(urllib.error.HTTPError) as caught:
                web_read.request("https://example.test/x", opener=opener)
            self.assertEqual(len(opener.calls), 1)
            self.assertEqual(web_read.failure(caught.exception)["kind"], kind)
        self.assertEqual(self.slept, [])

    def test_last_attempt_raises_the_original_error_with_its_kind_and_attempts(self):
        with self.assertRaises(urllib.error.URLError) as caught:
            web_read.request("https://example.test/x", opener=self.opener(urllib.error.URLError("no route")))
        detail = web_read.failure(caught.exception)
        self.assertEqual((detail["kind"], detail["attempts"]), ("network", 3))

    def test_rate_limit_waits_only_for_a_short_retry_after(self):
        opener = self.opener(_http(429, {"Retry-After": "5"}), _Response())
        web_read.request("https://example.test/x", opener=opener)
        self.assertEqual(self.slept, [5.0])
        opener = self.opener(_http(429, {"Retry-After": "3600"}), _Response())
        with self.assertRaises(urllib.error.HTTPError):
            web_read.request("https://example.test/x", opener=opener)
        self.assertEqual(len(opener.calls), 1)

    def test_single_attempt_spends_one_request(self):
        opener = self.opener(_http(503), _Response())
        with web_read.single_attempt(), self.assertRaises(urllib.error.HTTPError):
            web_read.request("https://example.test/x", opener=opener)
        self.assertEqual(len(opener.calls), 1)

    def test_host_breaker_and_deadline_stop_retries(self):
        down = urllib.error.URLError("down")
        for _ in range(3):
            with self.assertRaises(urllib.error.URLError):
                web_read.request("https://down.test/x", opener=self.opener(down))
        opener = self.opener(down)
        with self.assertRaises(urllib.error.URLError):
            web_read.request("https://down.test/y", opener=opener)
        self.assertEqual(len(opener.calls), 1, "the breaker is open for this host")
        opener = self.opener(down, _Response())
        self.now[0] = 301
        with self.assertRaises(urllib.error.URLError):
            web_read.request("https://other.test/x", opener=opener)
        self.assertEqual(len(opener.calls), 1, "past the deadline nothing is retried")

    def test_only_web_addresses_are_read(self):
        for url in ("file:///etc/hosts", "ftp://example.test/x", "data:text/plain,hi", "/etc/hosts"):
            opener = self.opener(_Response())
            with self.assertRaises(ValueError):
                web_read.request(url, opener=opener)
            self.assertEqual(opener.calls, [], url)

    def test_summary_counts_recoveries_and_failures_by_kind(self):
        entries = [{"ok": True, "attempts": 1}, {"ok": True, "attempts": 2}, {"ok": False, "kind": "challenged"}]
        self.assertEqual(web_read.summarise(entries), {"requests": 3, "recovered": 1, "failed": {"challenged": 1}})

    def test_script_rendered_shell_is_recognised(self):
        self.assertTrue(web_read.looks_script_rendered('<html><div id="root"></div><script src="a.js"></script></html>'))
        self.assertFalse(web_read.looks_script_rendered("<p>" + "words " * 60 + "</p><script></script>"))


class PublicAddressGuardTests(unittest.TestCase):
    """One owner for refusing a fetch of anything that is not a public internet address."""

    PRIVATE = [
        'http://127.0.0.1:8769/api/state',   # the live workspace on loopback
        'https://localhost/x', 'http://localhost.localdomain.localhost/',
        'http://169.254.169.254/latest/meta-data/',  # cloud metadata
        'http://metadata.google.internal/computeMetadata/v1/',
        'http://100.64.0.10:8769/api/state',  # the tailnet address
        'http://10.0.0.5/', 'http://192.168.1.1/', 'http://172.16.0.1/',
        'https://[::1]/', 'http://[fd00::1]/', 'http://[fe80::1]/',
        'ftp://example.com/x', 'file:///etc/passwd', 'https://u:p@example.com/',
    ]
    PUBLIC = ['https://example.com/x', 'http://example.com/a?b=c',
              'https://example.com:8443/api', 'https://93.184.216.34/']

    def test_private_loopback_and_metadata_addresses_are_refused(self):
        for url in self.PRIVATE:
            with self.subTest(url=url), self.assertRaises(ValueError):
                web_read.validate_public_url(url)

    def test_public_addresses_including_odd_ports_are_allowed(self):
        for url in self.PUBLIC:
            with self.subTest(url=url):
                self.assertEqual(web_read.validate_public_url(url)[0], url.split(':', 1)[0])

    def test_a_name_that_resolves_to_a_private_address_cannot_connect(self):
        # DNS rebinding: the name passes the literal check, the resolved address does not.
        with patch.object(web_read.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('10.0.0.1', 80))]):
            with self.assertRaises(ValueError):
                web_read.public_connection(('rebind.test', 80))

    def test_the_default_safe_opener_validates_direct_egress(self):
        with patch.object(web_read.urllib.request, 'getproxies', return_value={}):
            opener = web_read.safe_opener()
        handlers = {type(h).__name__ for h in opener.__self__.handlers}
        self.assertIn('PublicHTTPSHandler', handlers)
        self.assertIn('PublicHTTPHandler', handlers)


if __name__ == "__main__":
    unittest.main()
