"""Lossless, opt-in JSON string sharing for the browser's full summary.

Repeated source text can occur farther apart than gzip's 32 KiB window. A response-local table
shares it without dropping evidence, changing the logical API or retaining another state dict.
The decoder is js/summary-transport.js; ordinary JSON and versioned deltas stay unchanged.
"""
import json

FORMAT = 'strings-v1'
_MIN_LENGTH = 12


def encode(value, *, owned=False):
    """Return JSON bytes, preserving every value. '~N' references the response-local table;
    literal strings starting '~' get a second '~'. Keys and table entries are never encoded.

    `owned=True` hands over a tree the caller has just parsed and will not read again: it is
    packed in place, without a second copy of every container. Otherwise `value` is left untouched.
    """
    if not owned:
        value = json.loads(json.dumps(value))
    # Table order is first occurrence in document order: a pre-order walk, children left to right.
    counts = {}
    seen = counts.get
    stack = [value]
    pop, extend = stack.pop, stack.extend
    while stack:
        item = pop()
        kind = type(item)
        if kind is str:
            if len(item) >= _MIN_LENGTH:
                counts[item] = seen(item, 0) + 1
        elif kind is dict:
            extend(reversed(item.values()))
        elif kind is list:
            extend(reversed(item))
    strings = [text for text, occurrences in counts.items() if occurrences > 1]
    references = {text: '~' + str(index) for index, text in enumerate(strings)}
    reference = references.get

    def packed(text):
        shared = reference(text)
        if shared is not None:
            return shared
        return '~' + text if text[:1] == '~' else text

    if type(value) is str:
        value = packed(value)
    elif type(value) in (dict, list):
        stack.append(value)
        while stack:
            item = pop()
            for key, child in (item.items() if type(item) is dict else enumerate(item)):
                kind = type(child)
                if kind is str:
                    replacement = packed(child)
                    if replacement is not child:
                        item[key] = replacement
                elif kind is dict or kind is list:
                    stack.append(child)
    return json.dumps({'encoding': FORMAT, 'strings': strings, 'value': value},
                      separators=(',', ':')).encode()
