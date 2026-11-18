"""Read the existing observation object a member at a time, without a whole-file string.

JSONDecoder still owns JSON syntax. Only the root and its large items/sources maps are streamed;
peak temporary storage scales with the largest member, not the complete serialized archive.
"""
import json


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate object key {key!r}')
        result[key] = value
    return result


def read(source, *, sources_only=False, chunk_size=65536, visit_item=None):
    decoder = json.JSONDecoder(object_pairs_hook=_unique)
    buffer, ended = '', False

    def more():
        nonlocal buffer, ended
        part = source.read(chunk_size)
        buffer += part
        ended = not part

    def peek():
        nonlocal buffer
        while True:
            buffer = buffer.lstrip(' \t\r\n')
            if buffer or ended:
                return buffer[:1]
            more()

    def take(char):
        nonlocal buffer
        if peek() != char:
            raise ValueError(f'expected {char!r} in observation JSON')
        buffer = buffer[1:]

    def value():
        nonlocal buffer
        peek()
        while True:
            try:
                parsed, end = decoder.raw_decode(buffer)
                # A numeric token can continue across a chunk boundary.
                if ended or (end < len(buffer) and buffer[end] in ' \t\r\n,:]}'):
                    buffer = buffer[end:]
                    return parsed
            except json.JSONDecodeError:
                if ended:
                    raise
            more()

    def mapping(*, root=False, retain=True, items=False):
        take('{')
        result, seen = {}, set()
        if peek() == '}':
            if root and visit_item is not None:
                raise ValueError('missing observation items')
            take('}')
            return result
        while True:
            key = value()
            if not isinstance(key, str) or key in seen:
                raise ValueError('invalid or duplicate observation object key')
            seen.add(key)
            take(':')
            if root and key == 'items' and visit_item is not None and peek() != '{':
                raise ValueError('observation items must be an object')
            keep = retain and (not root or not sources_only or key == 'sources')
            if root and key in ('items', 'sources') and peek() == '{':
                parsed = mapping(retain=keep, items=key == 'items')
            else:
                parsed = value()
            if items and visit_item is not None:
                visit_item(key, parsed)
            if keep:
                result[key] = parsed
            # Release the last discarded advert before reading the next one.
            del parsed
            if peek() == '}':
                if root and visit_item is not None and 'items' not in seen:
                    raise ValueError('missing observation items')
                take('}')
                return result
            take(',')

    result = mapping(root=True)
    if peek():
        raise ValueError('trailing content in observation JSON')
    return result


def load(path, *, sources_only=False):
    """Compatibility reader for imports/status; only missing evidence is an empty start."""
    try:
        with path.open(encoding='utf-8') as source:
            return read(source, sources_only=sources_only)
    except FileNotFoundError:
        return {'sources': {}, 'items': {}}
