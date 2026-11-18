"""Shared email source identities; sender admission has one versioned configuration owner."""
import json
from pathlib import Path

SOURCES = json.loads((Path(__file__).resolve().parent.parent / 'config/email-sources.json').read_text())


def senders():
    return {kind: [source['sender']] for kind, source in SOURCES.items()}


def is_alert(source):
    source = str(source or '').removeprefix('source:')
    return source.startswith('alert:') and source[6:] in SOURCES


def label(source):
    return SOURCES.get(str(source or '').removeprefix('source:').removeprefix('alert:'), {}).get('label', 'Job alert email')
