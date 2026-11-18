"""Recent save, summary and build timings, held in this process's memory only.

The Dashboard's Responsiveness block and `tools/benchmark_save.py` read `snapshot()`. An entry is
a kind, a duration and optional phase durations: never a path, record id, query or content.
Nothing is written to disk or the database; a restart (every deploy) clears it.
"""
from __future__ import annotations

import collections
import datetime as dt
import statistics
import threading

# One bounded list per kind, so frequent background refreshes cannot push saves out.
# A summary refresh is counted by why the page asked (its X-WO-Refresh header): after a save, a
# background poll, or anything else. Cheap polls must not stand in for the wait after a save.
KINDS = ('save', 'refresh_after_save', 'refresh_background', 'refresh_other', 'summary_full', 'build')
PER_KIND = 50
_LOCK = threading.Lock()
_ENTRIES = {kind: collections.deque(maxlen=PER_KIND) for kind in KINDS}
_SINCE = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def record(kind: str, ms: float, phases: dict[str, float] | None = None) -> None:
    """Keep one timing. The lock covers an append, never I/O."""
    if kind not in _ENTRIES:
        raise ValueError(f'Unknown timing kind: {kind}')
    entry = (round(float(ms), 1), {k: round(float(v), 1) for k, v in (phases or {}).items()})
    with _LOCK:
        _ENTRIES[kind].append(entry)


def _spread(values: list[float]) -> dict:
    ordered = sorted(values)
    return dict(count=len(ordered), median_ms=round(statistics.median(ordered), 1),
                p90_ms=ordered[max(0, -(-len(ordered) * 9 // 10) - 1)], max_ms=ordered[-1])


def snapshot() -> dict:
    """Median, 90th percentile and maximum per kind, plus each phase's median, since the restart."""
    with _LOCK:
        copies = {kind: list(entries) for kind, entries in _ENTRIES.items()}
    kinds = {}
    for kind, entries in copies.items():
        if not entries:
            kinds[kind] = dict(count=0)
            continue
        kinds[kind] = _spread([ms for ms, _ in entries])
        names = sorted({name for _, phases in entries for name in phases})
        if names:
            kinds[kind]['phase_median_ms'] = {name: round(statistics.median(
                [phases[name] for _, phases in entries if name in phases]), 1) for name in names}
    return dict(since=_SINCE, per_kind=PER_KIND, kinds=kinds)


def clear() -> None:
    """Tests and benchmarks start from an empty record."""
    with _LOCK:
        for entries in _ENTRIES.values():
            entries.clear()
