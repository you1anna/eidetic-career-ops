"""Test-only server: real app/fixture, fixed date and deterministic synthetic IDs.

No production entry point imports this module. Each invocation builds a new disposable DB.
Wall-clock sleeps and HTTP timeouts retain their real clocks.
"""
import datetime as dt
import itertools
import shutil
from types import SimpleNamespace
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'workspace')]
FIXED = dt.datetime(2026, 11, 6, 12, tzinfo=dt.timezone.utc)


class FixedDate(dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 11, 6)


class FixedDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        value = FIXED.astimezone(tz) if tz else FIXED.replace(tzinfo=None)
        return cls.fromisoformat(value.isoformat())


if __name__ == '__main__':
    sequence = itertools.count()
    with patch.object(shutil,'disk_usage',return_value=SimpleNamespace(total=500*1024**3,free=300*1024**3,used=200*1024**3)), patch.object(dt, 'date', FixedDate), patch.object(dt, 'datetime', FixedDateTime), \
            patch.object(uuid, 'uuid4', lambda: uuid.uuid5(uuid.NAMESPACE_URL, f'visual-fixture-{next(sequence)}')):
        import make_fixture
        import server
        database = Path(sys.argv[1])
        make_fixture.build(database)
        import dashboard_fixture
        dashboard_fixture.write(database.parent)
        http = server.WorkspaceHTTPServer(('127.0.0.1', 0), server.Handler)
        http.db = database
        http.public_origin = None
        print(f'http://127.0.0.1:{http.server_port}', flush=True)
        try:
            http.serve_forever()
        finally:
            http.server_close()
