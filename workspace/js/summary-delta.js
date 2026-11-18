// After a save the server sends only what changed since this page's summary version
// (workspace_state.summary_json). The result equals a fresh full summary; anything unexpected
// throws, and the caller then reloads the whole summary.
const MISMATCH = "The update does not match this page's data.";

function patch(items, change) {
  const byId = new Map(items.map((item) => [item.id, item]));
  for (const id of change.remove) if (!byId.delete(id)) throw new Error(MISMATCH);
  for (const item of change.upsert) byId.set(item.id, item);
  const order = change.order || items.map((item) => item.id);
  if (order.length !== byId.size) throw new Error(MISMATCH);
  return order.map((id) => {
    if (!byId.has(id)) throw new Error(MISMATCH);
    return byId.get(id);
  });
}

// Returns a new state; unchanged entries are shared with the previous one, which is never modified.
export function applySummaryDelta(state, delta) {
  if (delta?.delta !== true || typeof delta.version !== "string" || typeof state?.version !== "string"
    || delta.base !== state.version || !state.intelligence) throw new Error(MISMATCH);
  const next = { ...state };
  for (const key of delta.unset) delete next[key];
  Object.assign(next, delta.set);
  const intelligence = { ...state.intelligence };
  for (const key of delta.intelligence.unset) delete intelligence[key];
  Object.assign(intelligence, delta.intelligence.set);
  next.records = patch(state.records, delta.collections.records);
  intelligence.actions = patch(state.intelligence.actions, delta.collections.actions);
  intelligence.research = patch(state.intelligence.research, delta.collections.research);
  next.intelligence = intelligence;
  next.version = delta.version;
  return next;
}
