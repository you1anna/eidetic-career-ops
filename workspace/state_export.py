"""Complete state transport without retaining the decoded receipt archive or response in RAM.

The public JSON shape/order is unchanged. SQLite snapshots end before a client receives bytes.
Private anonymous files bound incremental memory to the projection, one receipt and I/O buffers;
these are disposable caches, never an alternative historical store.
"""
from contextlib import contextmanager
import gzip
import json
import logging
import os
import shutil
import tempfile
import time

import record_views
import workspace_state

_CHUNK_SIZE = 64 * 1024
_FILE_CACHE = {}


def _json_chunks(conn, value, ensure_ascii):
    encoder = json.JSONEncoder(ensure_ascii=ensure_ascii)

    def members(items, coverage=False):
        yield '{'
        for n, (key, item) in enumerate(items):
            if n:
                yield ', '
            yield encoder.encode(key) + ': '
            if coverage and key == 'runs':
                yield '['
                for i, receipt in enumerate(record_views.iter_receipts(conn)):
                    if i:
                        yield ', '
                    yield encoder.encode(receipt)
                yield ']'
            elif key == 'coverage' and not coverage:
                yield from members(item.items(), coverage=True)
            else:
                yield from encoder.iterencode(item)
        yield '}'

    yield from members(value.items())


def _write_state(conn, stream, ensure_ascii):
    started = time.monotonic()
    with record_views.snapshot(conn):
        value = workspace_state.state_context(conn)
        parts, size = [], 0
        for chunk in _json_chunks(conn, value, ensure_ascii):
            parts.append(chunk)
            size += len(chunk)
            if size >= _CHUNK_SIZE:
                stream.write(''.join(parts).encode('utf-8'))
                parts, size = [], 0
        if parts:
            stream.write(''.join(parts).encode('utf-8'))
    length = stream.tell()
    stream.seek(0)
    if time.monotonic() - started >= workspace_state.STATE_BUILD_LOG_SECONDS:
        logging.info('workspace state exported bytes=%d dur_ms=%d cache=miss',
                     length, round((time.monotonic() - started) * 1000))
    # Projection and encoding buffers go out of scope before any client receives bytes.


@contextmanager
def state_json_file(conn, *, ensure_ascii=True):
    """Spool a complete coherent JSON response; failure produces no partial external output."""
    with tempfile.TemporaryFile(mode='w+b') as stream:
        _write_state(conn, stream, ensure_ascii)
        yield stream


def print_state(conn, output):
    """The existing `wo state` UTF-8 JSON plus newline, fully built before stdout is touched."""
    import codecs
    with state_json_file(conn, ensure_ascii=False) as stream:
        decoder = codecs.getincrementaldecoder('utf-8')()
        while chunk := stream.read(_CHUNK_SIZE):
            output.write(decoder.decode(chunk))
        output.write(decoder.decode(b'', final=True) + '\n')


class _Reader:
    """Independent offset and duplicated descriptor: eviction cannot close an active response.

    POSIX pread is available on both supported hosts (macOS and Linux). dup alone shares the
    original file offset, so ordinary seek/read would race between concurrent HTTP clients.
    """
    def __init__(self, stream, encoding):
        self.fd = os.dup(stream.fileno())
        self.length = os.fstat(self.fd).st_size
        self.encoding = encoding
        self.offset = 0

    def read(self, size=-1):
        if size < 0:
            size = self.length - self.offset
        value = os.pread(self.fd, size, self.offset)
        self.offset += len(value)
        return value

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class _Artifact:
    def __init__(self, raw, compressed):
        self.raw, self.compressed = raw, compressed

    def reader(self, compress):
        raw_size = os.fstat(self.raw.fileno()).st_size
        use_gzip = compress and raw_size > 1024 and os.fstat(self.compressed.fileno()).st_size < raw_size
        reader = _Reader(self.compressed if use_gzip else self.raw, 'gzip' if use_gzip else None)
        reader.varies = raw_size > 1024
        return reader

    def close(self):
        self.raw.close()
        self.compressed.close()


def _build_artifact(conn):
    raw = tempfile.TemporaryFile(mode='w+b')
    compressed = None
    try:
        compressed = tempfile.TemporaryFile(mode='w+b')
        _write_state(conn, raw, True)
        with gzip.GzipFile(fileobj=compressed, mode='wb', compresslevel=5, mtime=0) as zipped:
            shutil.copyfileobj(raw, zipped, _CHUNK_SIZE)
        compressed.flush()
        return _Artifact(raw, compressed)
    except BaseException:
        raw.close()
        if compressed is not None:
            compressed.close()
        raise


@contextmanager
def cached_state_file(conn, *, compress=False):
    """Single-build full-state disk cache; independent readers survive invalidation/eviction."""
    with workspace_state._BUILD_LOCK:
        key = workspace_state._data_key(conn, 'file')
        hit = _FILE_CACHE.get('full')
        if key is None or hit is None or hit[0] != key:
            old = _FILE_CACHE.pop('full', None)
            if old:
                old[1].close()
            hit = old = None
            artifact = _build_artifact(conn)
            if key is not None:
                _FILE_CACHE['full'] = (key, artifact)
        else:
            artifact = hit[1]
        try:
            reader = artifact.reader(compress)
        finally:
            if key is None:
                artifact.close()
    try:
        yield reader
    finally:
        reader.close()
