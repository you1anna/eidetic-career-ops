// Restore the exact logical summary before rendering, editing or applying versioned deltas.
// The response-local table shares strings only; decoded objects never alias one another.
export function decodeSummary(value) {
  if (!Object.hasOwn(value, 'encoding')) return value; // ordinary JSON / older server / delta
  const invalid = () => { throw new Error('Invalid summary encoding.'); };
  if (value.encoding !== 'strings-v1' || !Array.isArray(value.strings)
    || !value.strings.every(s => typeof s === 'string') || !Object.hasOwn(value, 'value')) invalid();
  const strings = value.strings;
  function unpack(item) {
    if (typeof item === 'string' && item.startsWith('~')) {
      if (item.startsWith('~~')) return item.slice(1);
      if (!/^~(0|[1-9][0-9]*)$/.test(item)) invalid();
      const i = Number(item.slice(1));
      if (!Number.isSafeInteger(i) || i >= strings.length) invalid();
      return strings[i];
    }
    if (Array.isArray(item)) return item.map(unpack);
    if (item && typeof item === 'object') {
      // Define own properties safely, including a literal '__proto__' from source data.
      return Object.fromEntries(Object.entries(item).map(([k, v]) => [k, unpack(v)]));
    }
    return item;
  }
  return unpack(value.value);
}
