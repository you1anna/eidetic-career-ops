"""Bounded, recorded context for evidence research; independent of outgoing wording."""
import profile_rules


def recorded_context(record):
    rules = profile_rules.current()
    lines = [line.strip() for line in (record.get('known_context') or '').splitlines()
             if line.strip() and not rules.is_private(line)]
    return '\n'.join(lines)[:2000]
