#!/usr/bin/env python3
"""Private, append-only raw-mail spool and bounded HTTP delivery endpoint.

One SQLite transaction owns receipt identity, metadata, MIME bytes and capacity accounting.
DELETE journaling and synchronous=FULL keep the receipt behind the durable commit boundary;
fullfsync is also requested on platforms that support it. A duplicate returns the first receipt.
No parsing checkpoint, quarantine, deletion, forwarding or inbox-reading HTTP API lives here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
from email import policy
from email.parser import BytesHeaderParser
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import uuid

import credentials

MAX_MESSAGE_BYTES = 512 * 1024
MAX_SPOOL_BYTES = 64 * 1024 * 1024
MAX_MESSAGES = 1000
RECIPIENT = "alerts@example.com"
READ_TIMEOUT = 5
_ADDRESS = re.compile(r"[^\s<>\"(),;\\@]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)")


class MailIntakeError(ValueError):
    """Public exceptions contain fixed, safe diagnostics, never input data or SQLite errors."""


class InvalidMailError(MailIntakeError):
    pass


class SpoolCapacityError(MailIntakeError):
    pass


class SpoolUnavailableError(MailIntakeError):
    pass


def validate_metadata(*, envelope_from: str, envelope_to: str, received_at: str) -> None:
    # The reverse path may be empty for delivery-status mail. It is evidence, not an auth claim.
    if (not isinstance(envelope_from, str) or len(envelope_from) > 320
            or any(ord(c) < 33 or ord(c) > 126 for c in envelope_from)
            or (envelope_from and not _ADDRESS.fullmatch(envelope_from))
            or envelope_to != RECIPIENT):
        raise InvalidMailError("invalid mail envelope")
    if not isinstance(received_at, str) or len(received_at) > 35 or not _TIMESTAMP.fullmatch(received_at):
        raise InvalidMailError("invalid receipt timestamp")
    try:
        dt.datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidMailError("invalid receipt timestamp") from None


def validate_raw(raw: bytes) -> None:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_MESSAGE_BYTES:
        raise InvalidMailError("invalid MIME size or type")
    # Validate the header/body boundary, not a provider-specific layout. Bad alert HTML must still
    # reach durable storage so the parser can quarantine and recover it independently.
    separator = re.search(br"\r?\n\r?\n", raw)
    if separator is None or separator.start() == 0 or separator.start() > 64 * 1024:
        raise InvalidMailError("invalid MIME header boundary")
    head = raw[:separator.start()]
    if b"\x00" in head:
        raise InvalidMailError("invalid MIME headers")
    try:
        message = BytesHeaderParser(policy=policy.default).parsebytes(head + b"\r\n\r\n")
        if message.defects or not message.keys():
            raise ValueError
    except Exception:
        raise InvalidMailError("invalid MIME headers") from None


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_private_parents(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        # A later fsync inside this directory does not persist its name in the ancestor.
        _sync_directory(directory.parent)


class MailSpool:
    def __init__(self, path: Path | str, max_bytes: int = MAX_SPOOL_BYTES, max_messages: int = MAX_MESSAGES,
                 *, readonly: bool = False):
        if type(max_bytes) is not int or max_bytes < 1 or type(max_messages) is not int or max_messages < 1:
            raise InvalidMailError("invalid spool capacity")
        self.path = Path(path).absolute()
        self.max_bytes, self.max_messages, self.readonly = max_bytes, max_messages, readonly
        try:
            if self.path.is_symlink():
                raise SpoolUnavailableError("spool path must be a private regular file")
            if readonly:
                if not self.path.is_file():
                    raise SpoolUnavailableError("spool is unavailable")
            elif not self.path.exists():
                _create_private_parents(self.path.parent)
                # SQLite must never create the file with the process's more permissive default umask.
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                self._initialize()
            if not self.path.is_file() or self.path.stat().st_mode & 0o077:
                raise SpoolUnavailableError("spool file must have private permissions")
            self.info()  # Existing unrelated, incomplete or corrupt files are not reinitialised.
        except MailIntakeError:
            raise
        except (OSError, sqlite3.Error):
            raise SpoolUnavailableError("spool is unavailable") from None

    @contextmanager
    def _connection(self, *, write=False):
        if write and self.readonly:
            raise SpoolUnavailableError("spool was opened read-only")
        connection = None
        try:
            mode = "rw" if write else "ro"
            connection = sqlite3.connect(self.path.as_uri() + "?mode=" + mode, uri=True,
                                         timeout=3, isolation_level=None)
            connection.row_factory = sqlite3.Row
            if write:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA fullfsync=ON")
            else:
                connection.execute("PRAGMA query_only=ON")
            yield connection
        except sqlite3.Error:
            raise SpoolUnavailableError("spool storage operation failed") from None
        finally:
            if connection is not None:
                connection.close()

    def _initialize(self):
        with self._connection(write=True) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE spool_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                               "version INTEGER NOT NULL, store_id TEXT NOT NULL)")
            connection.execute("INSERT INTO spool_metadata VALUES(1,1,?)", (str(uuid.uuid4()),))
            connection.execute("CREATE TABLE messages (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                               "receipt_id TEXT NOT NULL UNIQUE, received_at TEXT NOT NULL, "
                               "envelope_from TEXT NOT NULL, envelope_to TEXT NOT NULL, raw BLOB NOT NULL)")
            connection.commit()
        # Persist the initially created directory entry as well as SQLite's own durable commit.
        _sync_directory(self.path.parent)

    def info(self) -> dict:
        with self._connection() as connection:
            connection.execute("BEGIN")
            metadata = connection.execute("SELECT version, store_id FROM spool_metadata WHERE singleton=1").fetchone()
            if metadata is None or metadata["version"] != 1 or not metadata["store_id"]:
                raise SpoolUnavailableError("spool schema is unsupported")
            counts = connection.execute("SELECT COUNT(*) AS count, COALESCE(SUM(length(raw)),0) AS bytes, "
                                        "COALESCE(MAX(sequence),0) AS max_sequence FROM messages").fetchone()
            return {"store_id": metadata["store_id"], **dict(counts)}

    def receive(self, raw: bytes, *, envelope_from: str, envelope_to: str, received_at: str) -> dict:
        if self.readonly:
            raise SpoolUnavailableError("spool was opened read-only")
        validate_metadata(envelope_from=envelope_from, envelope_to=envelope_to, received_at=received_at)
        validate_raw(raw)
        digest = hashlib.sha256(raw).hexdigest()
        with self._connection(write=True) as connection:
            # Serialises duplicate checks, capacity checks and insertion across processes/threads.
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT sequence, received_at FROM messages WHERE receipt_id=?", (digest,)).fetchone()
            if previous is not None:
                connection.commit()
                return {"receipt_id": digest, "sequence": previous["sequence"],
                        "received_at": previous["received_at"], "duplicate": True}
            count, size = connection.execute("SELECT COUNT(*), COALESCE(SUM(length(raw)),0) FROM messages").fetchone()
            if count >= self.max_messages or size + len(raw) > self.max_bytes:
                raise SpoolCapacityError("spool capacity reached")
            cursor = connection.execute("INSERT INTO messages(receipt_id,received_at,envelope_from,envelope_to,raw) "
                                        "VALUES(?,?,?,?,?)", (digest, received_at, envelope_from, envelope_to, raw))
            sequence = cursor.lastrowid
            connection.commit()
        return {"receipt_id": digest, "sequence": sequence, "received_at": received_at, "duplicate": False}

    def list_messages(self, after: int = 0, limit: int = 200) -> list[dict]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidMailError("invalid spool cursor or limit")
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT sequence,receipt_id,received_at,envelope_from,envelope_to,raw FROM messages "
                "WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit))]

    def message(self, receipt_id: str) -> dict | None:
        """Read one original by its immutable content identity, never a path from the caller."""
        if not isinstance(receipt_id, str) or not re.fullmatch('[0-9a-f]{64}', receipt_id):
            raise InvalidMailError('invalid receipt identity')
        with self._connection() as connection:
            row = connection.execute('SELECT receipt_id,received_at,raw FROM messages WHERE receipt_id=?', (receipt_id,)).fetchone()
            if row and hashlib.sha256(row['raw']).hexdigest() != receipt_id:
                raise SpoolUnavailableError('original email failed its integrity check')
            return dict(row) if row else None


class IntakeServer(ThreadingHTTPServer):
    daemon_threads = True
    # Do not allow uncaught handler failures to print private request or exception material.
    def handle_error(self, request, client_address):
        pass


class IntakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mail-intake"
    sys_version = ""

    def setup(self):
        self.request.settimeout(READ_TIMEOUT)
        super().setup()

    def log_message(self, format, *args):
        pass

    def _json(self, status, payload):
        self.close_connection = True
        body = json.dumps(payload, separators=(",", ":")).encode("ascii")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (OSError, TimeoutError):
            # A committed receipt remains recoverable through the next identical POST.
            pass

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"error": "request_rejected"})

    def handle_expect_100(self):
        self._json(417, {"error": "expectation_not_supported"})
        return False

    def _one(self, name):
        values = self.headers.get_all(name, [])
        if len(values) != 1:
            raise InvalidMailError("missing or ambiguous request header")
        return values[0]

    def do_GET(self):
        if self.path != "/health":
            self._json(404, {"error": "not_found"})
            return
        try:
            info = self.server.spool.info()
        except Exception:
            self._json(503, {"status": "unavailable"})
            return
        if info["count"] >= self.server.spool.max_messages or info["bytes"] >= self.server.spool.max_bytes:
            self._json(503, {"status": "full"})
            return
        self._json(200, {"status": "ready"})

    def do_POST(self):
        if self.path != "/v1/email":
            self._json(404, {"error": "not_found"})
            return
        auth = self.headers.get_all("Authorization", [])
        candidate = auth[0].encode("utf-8") if len(auth) == 1 else b""
        if not hmac.compare_digest(candidate, self.server.bearer):
            self._json(401, {"error": "unauthorized"})
            return
        try:
            if self.headers.get_all("Transfer-Encoding"):
                raise InvalidMailError("transfer encoding is unsupported")
            length = self._one("Content-Length")
            if not re.fullmatch(r"[0-9]{1,10}", length):
                raise InvalidMailError("invalid content length")
            length = int(length)
            if length > MAX_MESSAGE_BYTES:
                self._json(413, {"error": "message_too_large"})
                return
            if length < 1:
                raise InvalidMailError("empty message")
            if self._one("Content-Type").lower().strip() != "message/rfc822":
                self._json(415, {"error": "unsupported_content_type"})
                return
            metadata = {"envelope_from": self._one("X-Alert-Envelope-From"),
                        "envelope_to": self._one("X-Alert-Envelope-To"),
                        "received_at": self._one("X-Alert-Received-At")}
            validate_metadata(**metadata)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise InvalidMailError("incomplete message")
            receipt = self.server.spool.receive(raw, **metadata)
        except InvalidMailError:
            self._json(400, {"error": "invalid_message"})
            return
        except (TimeoutError, socket.timeout):
            self._json(408, {"error": "request_timeout"})
            return
        except SpoolCapacityError:
            self._json(507, {"error": "spool_full"})
            return
        except Exception:
            self._json(503, {"error": "storage_unavailable"})
            return
        self._json(200 if receipt["duplicate"] else 201, receipt)


def make_server(spool: MailSpool, token: str, host: str = "127.0.0.1", port: int = 0) -> IntakeServer:
    if not isinstance(token, str) or not 32 <= len(token) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise InvalidMailError("receiver token must contain 32 to 512 printable non-space ASCII characters")
    server = IntakeServer((host, port), IntakeHandler)
    server.spool = spool
    server.bearer = ("Bearer " + token).encode("ascii")
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spool", type=Path, required=True, help="private SQLite spool file")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--token-file", type=Path, required=True, help="private file containing the receiver token")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        token = credentials.get(args.token_file.name, {"CREDENTIALS_DIRECTORY": str(args.token_file.absolute().parent)})
        server = make_server(MailSpool(args.spool), token, port=args.port)
    except (MailIntakeError, OSError):
        print("mail intake could not start; check private spool and token configuration", file=sys.stderr)
        return 1
    print(f"mail intake listening on 127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
