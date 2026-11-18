export const E = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined && text !== null) n.textContent = String(text);
  if (cls) n.className = cls;
  return n;
};
const URL_TEXT = /https?:\/\/[^\s<>"'`]+/gi;
export function extLink(href, text) {
  let u;
  try {
    u = new URL(href);
  } catch {
    return E("span", text);
  }
  if (!["http:", "https:"].includes(u.protocol)) return E("span", text);
  const a = E("a", text);
  a.href = u.href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}
export function linkify(node, text) {
  const s = String(text ?? "");
  let last = 0;
  for (const m of s.matchAll(URL_TEXT)) {
    const url = m[0].replace(/[.,;:!?)\]]+$/, "");
    if (m.index > last) node.append(s.slice(last, m.index));
    node.append(extLink(url, url));
    last = m.index + url.length;
  }
  if (last < s.length) node.append(s.slice(last));
  return node;
}
export const L = (tag, text, cls) => linkify(E(tag, null, cls), text);
// Card previews are buttons, so they show a host; full detail views use links.
export const shortUrls = (text) =>
  String(text ?? "").replace(URL_TEXT, (u) => {
    try {
      return new URL(u.replace(/[.,;:!?)\]]+$/, "")).hostname.replace(
        /^www\./,
        "",
      );
    } catch {
      return u;
    }
  });
export const A = (n, ...kids) => {
  n.append(...kids.flat().filter((x) => x instanceof Node));
  return n;
};
export const val = (x) =>
  x === null || x === undefined || x === "" ? "—" : String(x);
export const typeName = (s) =>
  String(s || "record")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
