#!/usr/bin/env python3
"""Append-once store for scan candidates.

Both scanners surface rows that must be proposed exactly once and must survive a retry. That is a
data-loss boundary, so it lives in one place with one set of tests rather than once per scanner.

A store directory holds:

    YYYY-MM-DD.tsv   rows first surfaced that day, retained across retries on the same day
    seen.json        every key ever surfaced, so a row is proposed once and never again
    last-run.json    per-source status and totals for the most recent run

The write order matters: the day's rows are saved **before** the dedupe index, so a crash between
the two loses nothing. The next run reads the day's TSV back, re-adds those keys to ``seen``, and
carries on — which is why ``load_day`` rejects a file it cannot fully understand instead of silently
dropping rows.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import datetime as dt
from pathlib import Path
from typing import Callable, Iterable

_WORKSPACE = str(Path(__file__).resolve().parent.parent / "workspace")
if _WORKSPACE not in sys.path:
    sys.path.append(_WORKSPACE)  # normalise: the one set of URL and name rules

# Free text, and the qualification derived from it, that nothing needs once the database holds the
# observation or no record depends on it; the short facts stay for lifecycle, coverage and matching.
COMPACTED_FIELDS = frozenset({"text", "summary", "description", "body", "qualification"})

# Written by ingest before the board scan and removed after it: the source evidence records depend on.
PROTECTION = "protected-sources.json"


def atomic_write(path: Path, content: str | Iterable[str]) -> None:
    """Replace a file in one step, so a reader never sees a half-written scan."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            if isinstance(content, str):
                f.write(content)
            else:
                f.writelines(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: dict) -> None:
    """Keep the existing format and atomic replacement without a serialized whole-store copy."""
    atomic_write(path, json.JSONEncoder(indent=2, sort_keys=True).iterencode(value))


def write_protection(path: Path, *, at: str, identities, urls, roles) -> None:
    """Source identities, canonical advert URLs and (organisation, title) keys that records hold."""
    atomic_write_json(Path(path), {"schema_version": 1, "generated_at": at,
                                   "identities": sorted([str(s), str(i)] for s, i in identities),
                                   "urls": sorted(set(urls)), "roles": sorted([str(o), str(t)] for o, t in roles)})


class Protection:
    """Adverts whose full text a scanner must keep, because a record depends on them.

    Only the matching ingest run supplies a manifest. Without a readable one (a standalone scan, an
    interrupted write) ``load`` returns ``None`` and the scanner keeps every advert whole.
    """

    def __init__(self, identities, urls, roles):
        self.identities = {(s, i) for s, i in identities}
        self.urls, self.roles = set(urls), {(o, t) for o, t in roles}

    @classmethod
    def load(cls, path: Path):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, "missing"
        except (OSError, ValueError):
            return None, "invalid"
        pairs = lambda rows: isinstance(rows, list) and all(
            isinstance(r, list) and len(r) == 2 and all(isinstance(x, str) for x in r) for r in rows)
        if (not isinstance(value, dict) or value.get("schema_version") != 1 or not pairs(value.get("identities"))
                or not pairs(value.get("roles")) or not isinstance(value.get("urls"), list)
                or not all(isinstance(u, str) for u in value["urls"])):
            return None, "invalid"
        return cls(value["identities"], value["urls"], value["roles"]), "ok"

    def holds(self, *, scope: str, source_id: str, url: str, company: str, title: str) -> bool:
        import normalise
        if (scope, source_id) in self.identities or (normalise.org_key(company), normalise.org_key(title)) in self.roles:
            return True
        if not url:
            return False
        try:
            return normalise.job_url(url) in self.urls
        except ValueError:
            return True


class PendingStore:
    """One day's candidates, the all-time seen index, and the last run's status."""

    def __init__(self, directory: Path, columns: list[str], key: str):
        self.dir = Path(directory)
        self.columns = list(columns)
        self.key = key
        self.dir.mkdir(parents=True, exist_ok=True)

    def day_path(self, day: str) -> Path:
        return self.dir / f"{day}.tsv"

    @property
    def seen_path(self) -> Path:
        return self.dir / "seen.json"

    def load_seen(self) -> set[str]:
        try:
            return set(json.loads(self.seen_path.read_text()))
        except (OSError, ValueError):
            return set()

    def load_day(self, day: str) -> list[dict]:
        """Rows an earlier attempt today already surfaced, so a retry adds to them.

        Raises on an unrecognised header or a malformed row. Failing loudly is the point: silently
        discarding rows we cannot parse would lose candidates that were already proposed.
        """
        path = self.day_path(day)
        if not path.exists():
            return []
        rows = []
        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            if not reader.fieldnames or self.key not in reader.fieldnames or set(reader.fieldnames) - set(self.columns):
                raise ValueError(f"Unrecognised header: {path}")
            for prev in reader:
                if None in prev or any(v is None for v in prev.values()) or not prev[self.key]:
                    raise ValueError(f"Malformed row: {path}")
                rows.append({c: prev.get(c, "") for c in self.columns})
        return rows

    def resume(self, day: str) -> tuple[list[dict], set[str]]:
        """Start-of-run state: today's rows so far, and every key ever surfaced including those."""
        rows = self.load_day(day)
        seen = self.load_seen()
        seen.update(r[self.key] for r in rows)
        return rows, seen

    def render(self, rows: list[dict]) -> str:
        """TSV text. Tabs and newlines inside a value are flattened so a row stays one line."""
        out = ["\t".join(self.columns)]
        for r in rows:
            out.append("\t".join(
                str(r.get(c, "")).replace("\t", " ").replace("\n", " ").replace("\r", " ")
                for c in self.columns))
        return "\n".join(out) + "\n"

    def save(self, day: str, rows: list[dict], seen: set[str], status: dict) -> None:
        # Candidates first: a crash after this point still leaves a recoverable day file.
        atomic_write(self.day_path(day), self.render(rows))
        atomic_write(self.seen_path, json.dumps(sorted(seen), indent=0))
        atomic_write(self.dir / "last-run.json", json.dumps(status, indent=2))

    def last_run(self) -> dict:
        try:
            return json.loads((self.dir / "last-run.json").read_text())
        except (OSError, ValueError):
            return {}


class ObservationStore:
    """Source-scoped current facts and conservative source lifecycle observations.

    This is deliberately separate from the append-once discovery TSV.  Consumers must not map
    ``source_lifecycle`` onto a person's workflow status.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        """Only a missing file is an empty start; unreadable evidence must never be replaced."""
        from observation_reader import read
        try:
            with self.path.open(encoding='utf-8') as source:
                value = read(source)
            self.validate(value)
        except FileNotFoundError:
            return {"schema_version": 1, "generated_at": None, "sources": {}, "items": {}}
        except ValueError as error:
            raise ValueError(f"Invalid observation store {self.path}: {error}") from error
        return value

    @staticmethod
    def validate(value: dict) -> None:
        """Validate the shapes the writer relies on, retaining unknown fields for other readers."""
        if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
            raise ValueError('unsupported schema_version (expected 1)')
        for section in ('sources', 'items'):
            if not isinstance(value.get(section), dict):
                raise ValueError(f'{section} must be an object')
            if any(not isinstance(row, dict) for row in value[section].values()):
                raise ValueError(f'{section} entries must be objects')
        for scope, source in value['sources'].items():
            if source.get('source_scope', scope) != scope:
                raise ValueError('source scope does not match its key')
            if 'item_ids' in source and (not isinstance(source['item_ids'], list) or
                                        any(not isinstance(x, str) for x in source['item_ids'])):
                raise ValueError('source item_ids must be a list of strings')
            if 'detail' in source and not isinstance(source['detail'], dict):
                raise ValueError('source detail must be an object')
        for key, item in value['items'].items():
            scope, sid = item.get('source_scope'), item.get('source_id')
            if not isinstance(scope, str) or not scope or not isinstance(sid, str) or not sid or key != ObservationStore.key(scope, sid):
                raise ValueError('item identity does not match its key')
            if not isinstance(item.get('current'), dict):
                raise ValueError('item current facts must be an object')
            if 'absence_observations' in item and (not isinstance(item['absence_observations'], list) or
                                                   any(not isinstance(x, str) for x in item['absence_observations'])):
                raise ValueError('item absence_observations must be a list of strings')

    @staticmethod
    def key(scope: str, source_id: str) -> str:
        return f"{scope}\x1f{source_id}"

    def compact(self, at: str, *, held: Callable[[dict], bool], closed_days: int = 7,
                unseen_days: int = 30) -> int:
        """Drop the advert text of jobs and tenders closed ``closed_days`` ago or unseen for
        ``unseen_days``, when ``held(item)`` says the database holds that exact observation.
        Identity, lifecycle and the short facts stay, so a closure or a later sighting still works."""
        data = self.load()
        now = dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
        def older(stamp, days):
            try:
                return now - dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) >= dt.timedelta(days=days)
            except ValueError:
                return False
        count = 0
        for item in data["items"].values():
            if item.get("compacted_at") or item.get("kind") not in ("job", "tender"):
                continue
            if not set(item["current"]) & COMPACTED_FIELDS:
                continue
            stale = (older(item.get("closed_at"), closed_days) if item.get("source_lifecycle") == "closed"
                     else older(item.get("last_seen_at"), unseen_days))
            if stale and held(item):
                item["current"] = {k: v for k, v in item["current"].items() if k not in COMPACTED_FIELDS}
                item["compacted_at"] = at
                count += 1
        if count:
            atomic_write_json(self.path, data)
        return count

    def observe(self, *, scope: str, source_type: str, provider: str, observed_at: str,
                state: str, complete: bool, items: list[dict], detail: dict | None = None,
                absence_authoritative: bool = True, expired_on: str | None = None) -> dict:
        """Record one read of a source. ``absence_authoritative=False`` is for sources that only
        report presence (a moving search window, an alert email): absence never closes their items.
        ``expired_on`` (an ISO date) closes items whose source-stated ``expires`` date has passed,
        whether or not the read returned them. An item may supply ``first_seen_at`` when a bounded
        batch coalesces earlier sightings; persist that chronology with its current facts atomically."""
        data = self.load()
        now = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        current_ids = {str(x["source_id"]) for x in items if x.get("source_id")}
        previous = [v for v in data["items"].values() if v.get("source_scope") == scope and
                    v.get("source_lifecycle") != "closed"]
        # A suddenly empty source that previously carried records is not enough evidence to mutate
        # lifecycle. It is retained as an anomalous source receipt for investigation.
        anomalous_empty = bool(absence_authoritative and complete and not current_ids and previous)
        effective_complete = bool(complete and not anomalous_empty and state in ("ok", "empty"))
        run_id = f"{scope}@{observed_at}"
        data["sources"][scope] = {
            "source_scope": scope, "source_type": source_type, "provider": provider,
            "complete": effective_complete, "reported_complete": bool(complete),
            "state": "anomalous-empty" if anomalous_empty else state,
            "observed_at": observed_at, "run_id": run_id, "item_ids": sorted(current_ids),
            "detail": detail or {},
        }
        for item in items:
            sid = str(item.get("source_id") or "")
            if not sid:
                continue
            k = self.key(scope, sid)
            old = data["items"].get(k, {})
            first_seen = item.get('first_seen_at') or observed_at
            first_time = dt.datetime.fromisoformat(first_seen.replace('Z', '+00:00'))
            if first_time > now:
                raise ValueError('First sighting cannot follow the current observation')
            if old.get('first_seen_at') and dt.datetime.fromisoformat(old['first_seen_at'].replace('Z', '+00:00')) < first_time:
                first_seen = old['first_seen_at']
            explicit = item.get("explicit_closure") or None
            lifecycle = "closed" if explicit else "active"
            # A scanner's compaction_reason marks an advert no record needs: keep identity, lifecycle
            # and short facts, as compact() would, from the first sighting rather than weeks later.
            reason = item.get("compaction_reason")
            current = item.get("current") or {}
            if reason:
                current = {f: v for f, v in current.items() if f not in COMPACTED_FIELDS}
            data["items"][k] = {
                "source_scope": scope, "source_id": sid, "provider": provider,
                "kind": source_type, "current": current,
                "first_seen_at": first_seen,
                "last_seen_at": observed_at,
                "last_complete_seen_at": observed_at if effective_complete else old.get("last_complete_seen_at"),
                "source_lifecycle": lifecycle,
                "explicit_closure": explicit,
                "absence_observations": [],
                "closed_at": observed_at if explicit else None,
                "closure_reason": (explicit or {}).get("reason") if explicit else None,
            }
            if reason:
                data["items"][k].update(compacted_at=observed_at, compaction_reason=str(reason))
        if effective_complete and absence_authoritative:
            for old in previous:
                sid = old["source_id"]
                if sid in current_ids:
                    continue
                k = self.key(scope, sid)
                rec = data["items"][k]
                absences = list(rec.get("absence_observations") or [])
                if observed_at not in absences:
                    absences.append(observed_at)
                rec["absence_observations"] = absences
                rec["source_lifecycle"] = "absent"
                if len(absences) >= 2:
                    first = dt.datetime.fromisoformat(absences[0].replace("Z", "+00:00"))
                    if now - first >= dt.timedelta(hours=24):
                        rec["source_lifecycle"] = "closed"
                        rec["closed_at"] = observed_at
                        rec["closure_reason"] = "absent-from-two-complete-source-observations-24h-apart"
        if expired_on:
            for rec in data["items"].values():
                expires = str(rec.get("current", {}).get("expires") or "")
                if (rec.get("source_scope") == scope and rec.get("source_lifecycle") != "closed"
                        and expires and expires < expired_on):
                    reason = f"source-expiry:{expires}"
                    rec.update(source_lifecycle="closed", closed_at=observed_at, closure_reason=reason,
                               explicit_closure={"reason": reason})
        data["generated_at"] = observed_at
        atomic_write_json(self.path, data)
        return data
