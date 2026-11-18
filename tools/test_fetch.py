"""The attended fetch chain: when it falls back, and when it stops."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from email.message import Message

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch  # noqa: E402
import web_read  # noqa: E402


def _http(code, headers=None):
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://site.test/x", code, "status", msg, None)


def _page(body, content_type="text/html; charset=utf-8"):
    return {"status": 200, "url": "https://site.test/x", "headers": {"Content-Type": content_type},
            "body": body.encode(), "charset": "utf-8"}


class FetchChainTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"WO_FETCH_LOG": str(Path(tmp.name) / "fetch.jsonl")})
        env.start()
        self.addCleanup(env.stop)
        patch.object(fetch, "robots_allows", return_value=True).start()
        self.remote = patch.object(fetch, "remote_read").start()
        self.browser = patch.object(fetch, "browser_read").start()
        self.addCleanup(patch.stopall)

    def direct(self, outcome):
        return patch.object(fetch.web_read, "request", side_effect=[outcome] if isinstance(outcome, BaseException) else None,
                            return_value=None if isinstance(outcome, BaseException) else outcome)

    def test_a_bot_challenge_stops_the_chain_with_what_to_do_instead(self):
        error = _http(403, {"cf-mitigated": "challenge"})
        with self.direct(error):
            code, content, notes = fetch.run("https://site.test/feed", remote="ops@vps")
        self.assertEqual((code, content), (2, ""))
        self.remote.assert_not_called()
        self.browser.assert_not_called()
        self.assertIn("API", notes[-1])

    def test_an_unreachable_source_is_tried_from_the_vps_without_recursion(self):
        error = urllib.error.URLError("no route")
        error.fetch_kind, error.fetch_attempts = "network", 3
        self.remote.return_value = subprocess.CompletedProcess([], 0, "remote text\n", "fetch: read directly on vps")
        with self.direct(error):
            code, content, _ = fetch.run("https://site.test/x", remote="ops@vps")
        self.assertEqual((code, content), (0, "remote text\n"))
        self.remote.assert_called_once_with("https://site.test/x", "ops@vps", "text", False)

    def test_a_block_on_the_vps_is_reported_as_a_block(self):
        self.remote.return_value = subprocess.CompletedProcess([], 2, "", "fetch: direct read on vps: challenged")
        with self.direct(urllib.error.URLError("no route")):
            code, _, _ = fetch.run("https://site.test/x", remote="ops@vps")
        self.assertEqual(code, 2)

    def test_an_unreachable_vps_is_a_note_not_a_crash(self):
        self.remote.side_effect = subprocess.TimeoutExpired("ssh", 180)
        code, _, notes = fetch.run("https://site.test/x", via="vps", remote="ops@vps")
        self.assertEqual(code, 1)
        self.assertIn("VPS route failed", notes[0])

    def test_without_a_vps_route_an_unreachable_source_is_a_plain_failure(self):
        with self.direct(urllib.error.URLError("no route")):
            code, _, _ = fetch.run("https://site.test/x", remote="")
        self.assertEqual(code, 1)
        self.remote.assert_not_called()

    def test_a_script_rendered_shell_goes_to_the_browser(self):
        self.browser.return_value = {"kind": None, "status": 200, "text": "Rendered roles", "html": "<p>Rendered roles</p>"}
        with self.direct(_page('<div id="app"></div><script src="app.js"></script>')):
            code, content, _ = fetch.run("https://site.test/jobs", remote="")
        self.assertEqual((code, content), (0, "Rendered roles"))

    def test_without_a_browser_the_shell_text_is_returned_with_a_warning(self):
        self.browser.side_effect = fetch.BrowserUnavailable("node is not installed")
        with self.direct(_page('<title>Jobs</title><div id="app"></div><script src="app.js"></script>')):
            code, content, notes = fetch.run("https://site.test/jobs", remote="")
        self.assertEqual((code, content), (0, "Jobs"))
        self.assertTrue(any("incomplete" in n for n in notes))

    def test_a_failed_browser_read_still_returns_the_shell_text(self):
        self.browser.side_effect = RuntimeError("navigation timeout")
        with self.direct(_page('<title>Jobs</title><div id="app"></div><script src="app.js"></script>')):
            code, content, _ = fetch.run("https://site.test/jobs", remote="")
        self.assertEqual((code, content), (0, "Jobs"))

    def test_a_browser_challenge_is_reported_not_worked_around(self):
        self.browser.return_value = {"kind": "challenged", "status": 403, "text": "", "html": ""}
        code, _, _ = fetch.run("https://site.test/jobs", browser=True, remote="ops@vps")
        self.assertEqual(code, 2)
        self.remote.assert_not_called()

    def test_robots_disallow_prevents_any_read(self):
        with patch.object(fetch, "robots_allows", return_value=False), self.direct(_page("<p>x</p>")) as direct:
            code, _, notes = fetch.run("https://site.test/private", remote="")
        self.assertEqual(code, 2)
        direct.assert_not_called()
        self.assertIn("robots.txt", notes[0])

    def test_local_files_are_refused_on_every_route(self):
        for browser, via in ((False, "auto"), (True, "auto"), (False, "vps")):
            code, content, _ = fetch.run("file:///srv/work-ops/ingest.env", browser=browser, via=via, remote="ops@vps")
            self.assertEqual((code, content), (1, ""))
        self.browser.assert_not_called()
        self.remote.assert_not_called()

    def test_private_and_loopback_addresses_are_refused_before_any_route(self):
        # The live workspace on loopback, the tailnet address and the cloud metadata endpoint are not
        # public sources; refuse them on every route, without a direct read, browser or VPS relay.
        for url in ("http://127.0.0.1:8769/api/state", "http://100.64.0.10:8769/api/state",
                    "http://169.254.169.254/latest/meta-data/", "http://localhost/x"):
            for browser, via in ((False, "auto"), (True, "auto"), (False, "vps")):
                with self.subTest(url=url, browser=browser, via=via), self.direct(_page("<p>x</p>")) as direct:
                    code, content, notes = fetch.run(url, browser=browser, via=via, remote="ops@vps")
                self.assertEqual((code, content), (1, ""))
                direct.assert_not_called()
                self.assertTrue(any("refused" in n for n in notes))
        self.browser.assert_not_called()
        self.remote.assert_not_called()

    def test_json_pretty_prints(self):
        with self.direct(_page('{"a": 1}', "application/json")):
            code, content, _ = fetch.run("https://api.test/x", fmt="json", remote="")
        self.assertEqual((code, content), (0, '{\n  "a": 1\n}'))

    def test_the_remote_command_is_quoted_for_the_remote_shell(self):
        patch.stopall()  # the real remote_read, with ssh itself stubbed
        with patch.object(fetch.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            fetch.remote_read("https://site.test/x?a=1&b=$(id)", "ops@vps", "text", False)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"])
        self.assertIn("'https://site.test/x?a=1&b=$(id)'", argv[-1])
        self.assertIn("--via local", argv[-1])

    def test_only_the_mac_has_a_default_vps_route(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WO_FETCH_REMOTE", None)
            with patch.object(fetch.sys, "platform", "linux"):
                self.assertEqual(fetch.default_remote(), "")
            with patch.object(fetch.sys, "platform", "darwin"):
                self.assertTrue(fetch.default_remote())


if __name__ == "__main__":
    unittest.main()
