#!/usr/bin/env python3
"""Synthetic local Wrangler email() -> real Python spool round trip; no remote services."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time

DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(DIRECTORY.parents[1] / "tools"))
from mail_intake import MailSpool, make_server  # noqa: E402

RAW = (b"From: jobalerts-noreply@linkedin.com\r\nTo: alerts@example.com\r\n"
       b"Message-ID: <synthetic-local-bridge@example.test>\r\nSubject: Synthetic bridge check\r\n"
       b"Content-Type: text/plain; charset=utf-8\r\n\r\nSynthetic local data only.\r\n")


def private_write(path: Path, text: str) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(text)


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port: int, method: str, path: str, body: bytes | None = None) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request(method, path, body=body, headers={"Content-Type": "message/rfc822"})
        response = connection.getresponse()
        response.read(4096)  # The simulator's diagnostic body is never printed.
        return response.status
    finally:
        connection.close()


def run() -> dict:
    wrangler = DIRECTORY / "node_modules" / ".bin" / "wrangler"
    if not wrangler.is_file():
        raise RuntimeError("Run npm ci in ops/email-intake first")
    with tempfile.TemporaryDirectory(prefix="work-ops-email-bridge-") as temp:
        root = Path(temp)
        token = secrets.token_urlsafe(36)
        private_write(root / "intake-token", token)
        private_write(root / ".dev.vars", "\n".join((
            "INTAKE_TOKEN=" + token,
            "CF_ACCESS_CLIENT_ID=synthetic-local-access-id",
            "CF_ACCESS_CLIENT_SECRET=synthetic-local-access-secret",
        )) + "\n")
        spool = MailSpool(root / "spool" / "mail.db")
        receiver = make_server(spool, token)
        receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
        receiver_thread.start()
        running = True
        port = unused_port()
        config = json.loads((DIRECTORY / "wrangler.jsonc").read_text())
        config["main"] = str(DIRECTORY / "src" / "index.ts")
        config["vars"] = {
            "INTAKE_URL": f"http://127.0.0.1:{receiver.server_address[1]}/v1/email",
            "ALLOW_LOOPBACK_HTTP": "true",
        }
        private_write(root / "wrangler.jsonc", json.dumps(config))
        private_write(root / "runtime.log", "")
        process = None
        try:
            with (root / "runtime.log").open("w") as log:
                process = subprocess.Popen([
                    str(wrangler), "dev", "--local", "--ip", "127.0.0.1", "--port", str(port),
                    "--inspector-port", "0", "--config", str(root / "wrangler.jsonc"),
                    "--persist-to", str(root / "state"), "--log-level", "error",
                ], cwd=DIRECTORY, stdout=log, stderr=subprocess.STDOUT,
                    env=os.environ | {"WRANGLER_SEND_METRICS": "false"})
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Local Wrangler exited before becoming ready")
                    try:
                        if request(port, "GET", "/") == 404:
                            break
                    except (OSError, http.client.HTTPException):
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Local Wrangler did not become ready")
                    time.sleep(0.1)
                route = "/cdn-cgi/local/email?from=forwarder%40example.test&to=alerts%40portfolio.example.com"
                first = request(port, "POST", route, RAW)
                if first != 200:
                    raise RuntimeError("Synthetic Worker delivery did not succeed")
                stored = spool.list_messages()
                if len(stored) != 1 or stored[0]["raw"] != RAW or stored[0]["receipt_id"] != hashlib.sha256(RAW).hexdigest():
                    raise RuntimeError("Synthetic receipt does not match delivered bytes")
                duplicate = request(port, "POST", route, RAW)
                if duplicate != 200 or spool.list_messages() != stored:
                    raise RuntimeError("Duplicate delivery changed the stored receipt")
                receiver.shutdown()
                receiver.server_close()
                receiver_thread.join(timeout=5)
                running = False
                unavailable = request(port, "POST", route, RAW + b"new delivery")
                if unavailable < 400 or spool.list_messages() != stored:
                    raise RuntimeError("Unavailable receiver was incorrectly acknowledged")
                return {"first_delivery": first, "duplicate_delivery": duplicate,
                        "unavailable_delivery": unavailable, "retained_messages": 1,
                        "raw_sha_matches": True, "duplicate_unchanged": True}
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if running:
                receiver.shutdown()
                receiver.server_close()
                receiver_thread.join(timeout=5)


if __name__ == "__main__":
    try:
        print(json.dumps(run(), sort_keys=True))
    except Exception as error:
        # This script handles synthetic data only, but still never prints subprocess logs/secrets.
        print(f"Local email bridge check failed ({type(error).__name__})", file=sys.stderr)
        raise SystemExit(1)
