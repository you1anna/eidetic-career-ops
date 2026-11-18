// An open page re-reads run status (`/api/status`, not the whole summary) so its age labels judge
// current runs: when the owner returns to it, and every five minutes while it is visible. Focus and
// visibility both fire on a return, so a return within 30 seconds of a read does not read again.
export const RETURN_GAP_MS = 30 * 1000, VISIBLE_GAP_MS = 5 * 60 * 1000;
export function statusReadDue(lastReadAt, now, { returning = false, hidden = false } = {}) {
  return !hidden && now - lastReadAt >= (returning ? RETURN_GAP_MS : VISIBLE_GAP_MS);
}
// Swap a rebuilt status block in place, keeping each section the reader had open (matched by its
// first class, else its heading). Status blocks hold no form fields, so nothing typed is lost.
export function replaceKeepingOpen(old, next) {
  const sections = n => [...(n.matches("details") ? [n] : []), ...n.querySelectorAll("details")];
  const key = d => d.classList[0] || d.querySelector(":scope > summary")?.textContent;
  const open = new Set(sections(old).filter(d => d.open).map(key));
  for (const d of sections(next)) if (open.has(key(d))) d.open = true;
  old.replaceWith(next);
}
