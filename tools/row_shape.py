"""The flat record row every import builds, and the daily-scan TSV reader.

`row()` gives a record dictionary with every text column, so an import can pass it to
`wo.upsert_record`. `scan_rows()` turns the legacy daily-scan TSVs in a pending folder into job
rows (SCAN-<hash of URL>); modern scanners write observations instead, and the TSVs remain the
fallback for rows without one.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

COLUMNS = ["id", "type", "name", "organisation", "role", "subtype", "status", "engagement", "location", "workplace", "url",
           "contact", "pay", "pay_min", "pay_max", "pay_currency", "pay_period", "ir35", "date", "last_touch", "next_action",
           "verification", "related_ids", "notes", "source_file", "verification_level",
           "record_url", "detail_url", "apply_url", "evidence_urls", "summary", "email", "phone",
           "action", "action_state", "action_owner", "action_due", "related_names"]
# The career-ops layout the scan TSVs were first written under; a folder inside it keeps that prefix
# in `source_file`, so older records keep their provenance.
BASE = "commercial/career-ops-user"


def clean(s: str) -> str:
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", s or "")
    s = s.replace("**", "").replace("`", "")
    s = re.sub(r"\s+", " ", s).strip()
    return "" if s in ("—", "-", "–") else s


def join(*parts: str, sep: str = " | ") -> str:
    return sep.join(p for p in (clean(x) for x in parts) if p)


def slug(name: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s if len(s) <= limit else s[:limit].rsplit("-", 1)[0]


def scan_id(url: str) -> str:
    return "SCAN-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def row(**fields) -> dict:
    return {c: fields.get(c, "") or "" for c in COLUMNS}


def scan_tsv(path: Path) -> list[dict]:
    """One daily-scan TSV as raw column dictionaries; rows with a wrong column count are dropped."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    cols = lines[0].split("\t")
    return [dict(zip(cols, cells)) for cells in (line.split("\t") for line in lines[1:]) if len(cells) == len(cols)]


def scan_rows(folder: Path, source_prefix: str, inventory: list[tuple[Path, list[dict]]] | None = None) -> list[dict]:
    """Normalize dated rows, optionally reusing the caller's parsed inventory for this import."""
    rows, seen = [], set()
    if inventory is None:
        inventory = [(path, scan_tsv(path)) for path in sorted(folder.glob("????-??-??.tsv"))]
    for path, source_rows in inventory:
        for j in source_rows:
            url = j.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            pay = {k: j.get(k, "") for k in ("pay_min", "pay_max", "pay_currency", "pay_period")}
            rows.append(row(id=scan_id(url), type="job", name=j.get("title"), organisation=j.get("company"), subtype="daily scan",
                            status="scan candidate — untriaged", engagement=j.get("engagement"), location=j.get("location"),
                            workplace=j.get("workplace"), url=url, pay=j.get("rate") if j.get("rate") != "unstated" else "",
                            ir35=j.get("ir35"), date=j.get("posted"), verification="public ATS API, rule-based daily scan",
                            notes=join(f"seen {j.get('date_seen', '')}", f"provider {j.get('provider', '')}", j.get("employment_type", ""),
                                       j.get("department", ""), j.get("why", ""), f"pay from {j['pay_source']}" if j.get("pay_source") else ""),
                            source_file=f"{source_prefix}/{path.name}", **pay))
    return rows
