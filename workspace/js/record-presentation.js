// Shared record presentation: no application state, navigation or network requests.
import { E, A, shortUrls, val, typeName } from "./dom.js";
import { FIELD_NAMES, RELATION_NAMES, VALUE_NAMES, KIND_NAMES, BAND_NAMES, ENGAGEMENT_NAMES } from "./labels.js";

const stateName = (s) => typeName(s || "needs_review");
const fieldName = (k) =>
  FIELD_NAMES[k] ||
  typeName(k).replace(/^(\w)(.*)$/, (m, a, b) => a + b.toLowerCase());
const VOCAB_FIELDS = new Set([
  "stage",
  "status",
  "relationship",
  "kind",
  "working_state",
  "source_lifecycle",
]);
const fieldValue = (k, v) =>
  VALUE_NAMES[k]?.[v] || (k === "kind" && KIND_NAMES[v]) || (VOCAB_FIELDS.has(k) ? stateName(v) : String(v));
const relationName = (l, id) =>
  (RELATION_NAMES[l.relation] || [typeName(l.relation), typeName(l.relation)])[
    l.from_id === id ? 0 : 1
  ];
const urlLabel = (u) => {
  const path = u.pathname.replace(/\/$/, "");
  return (
    u.hostname.replace(/^www\./, "") +
    (path ? (path.length > 32 ? path.slice(0, 31) + "…" : path) : "")
  );
};
function chip(text, key = "") {
  return E("span", text, "chip " + (key ? "state-" + key : ""));
}
function section(title, count) {
  const h = E("div", null, "section-head");
  h.append(
    E("h2", title),
    count !== undefined
      ? E("span", count, "count")
      : document.createTextNode(""),
  );
  return h;
}
function factualDate(s) {
  if (!s) return "—";
  const d = new Date(s);
  return Number.isNaN(d.valueOf())
    ? String(s)
    : d.toLocaleDateString("en-GB", {
        day: "numeric",
        month: "short",
        year: "numeric",
      });
}
// Free-text dates such as "Aug 2026" stay as written rather than gaining an invented day.
function statedDate(s) {
  return /^\d{4}-\d{2}-\d{2}/.test(String(s ?? "")) ? factualDate(s) : String(s ?? "");
}
function recordState(r) {
  return r.working_state || r.followup?.status || "needs_review";
}
function canonicalAction(r) {
  return r.next_action || r.followup?.action || "";
}
function commercialState(r) {
  const v = r.stage || r.relationship || r.status || r.kind || "";
  return v === r.type ? "" : v;
}
function latestActivity(r) {
  return (
    r.last_activity ||
    (/^\d{4}-\d{2}-\d{2}/.test(r.last_touch || "") ? r.last_touch : "")
  );
}
function activityTime(r) {
  const raw = latestActivity(r);
  if (!/^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+-Z]+)?$/.test(String(raw || "")))
    return 0;
  const n = Date.parse(raw);
  return Number.isFinite(n) ? n : 0;
}
function contextLine(r) {
  const research = r.research?.[0] || {},
    raw =
      r.type === "company"
        ? r.summary ||
          r.known_context ||
          r.signals?.[0]?.headline ||
          "No description yet."
        : r.summary ||
          r.known_context ||
          r.notes ||
          research.summary ||
          research.desk_summary ||
          research.signal ||
          research.mandate_evidence ||
          canonicalAction(r) ||
          r.status_reason ||
          "No context recorded.";
  return firstSentence(raw);
}
// The first sentence, up to 220 characters; an initial such as "C. elegans" does not end it.
function firstSentence(raw) {
  const clean = shortUrls(raw).replace(/\s+/g, " ").trim(),
    sentence =
      clean.match(/^.{1,220}?(?:(?<!(?:^|[\s(])[A-Z])[.!?](?=\s|$)|$)/)?.[0] ||
      clean.slice(0, 220);
  return sentence.length < clean.length && !/[.!?]$/.test(sentence)
    ? sentence + "…"
    : sentence;
}
function rel(r) {
  return r.relevance || { score: 0, band: "low", reasons: [] };
}
function relSort(a, b) {
  return (
    rel(b).score - rel(a).score ||
    activityTime(b) - activityTime(a) ||
    val(a.name).localeCompare(val(b.name), "en-GB")
  );
}
function bandChip(r) {
  const b = rel(r).band;
  return E("span", BAND_NAMES[b] || "Lower", "chip band band-" + b);
}

function choiceLabel(d, id) {
  return d.choices.find((c) => c.id === id)?.label || id;
}

function hostOf(u) {
  try {
    return new URL(u).hostname.replace(/^www\./, "");
  } catch {
    return u;
  }
}

function lane(r) {
  return rel(r).lane || "";
}

function engagementLabel(r) {
  // Qualification owns the effective terms. Rendering must not restore an old stored claim
  // when the source assessment is unknown or conflicting, or mutate either source of evidence.
  const qualification = rel(r).qualification;
  if (qualification?.conflicts?.length) return "Engagement unresolved · source terms conflict";
  const engagement = String(qualification?.engagement ?? r.engagement ?? "").trim().toLowerCase();
  if (!engagement || engagement === "unknown") return "Engagement not stated";
  return ENGAGEMENT_NAMES[engagement] || typeName(engagement);
}

// Shared card/detail facts only; ranking and qualification remain server-owned.
export function cardRemit(record) {
  const remit = (record.relevance?.fit?.evidence || []).find(item => {
    if (item.kind !== 'remit' || typeof item.quote !== 'string' || !item.quote.trim()) return false;
    try { return ['http:', 'https:'].includes(new URL(item.url).protocol); } catch { return false; }
  });
  return remit ? {text: remit.quote, url: remit.url} : {text: firstSentence(record.summary || ''), url: ''};
}

export function jobLocation(record) {
  const location = String(record.location || '').trim() || 'Location not stated';
  const workplace = String(record.workplace || '').toLowerCase();
  const label = {hybrid:'Hybrid', onsite:'On-site', remote:'Remote'}[workplace];
  const stated = {hybrid:/\bhybrid\b/i, onsite:/\bon[ -]?site\b/i, remote:/\bremote\b/i}[workplace];
  // Retain source attendance conditions verbatim; a location alone cannot establish workplace.
  return label && !stated.test(location) ? `${location} · ${label}` : location;
}

export function jobDecisionFacts(record) {
  const remit = cardRemit(record), engagement = engagementLabel(record);
  const fit = (record.relevance?.reasons || []).find(reason => reason.fit && reason.weight > 0 && reason.text);
  const uncertainty = record.relevance?.fit?.gaps?.[0] || record.relevance?.qualification?.conflicts?.[0]
    || record.briefing?.decisive_unknowns?.[0] || '';
  const missing = [];
  if (!remit.text) missing.push('work and requirements not established');
  if (!record.pay && engagement === 'Engagement not stated') missing.push('pay and engagement not stated');
  return {remit, pay:record.pay || 'Pay not stated', engagement, location:jobLocation(record),
    fit:fit?.text || 'Not established from recorded evidence.', uncertainty, missing};
}

function laneChip(r) {
  const l = lane(r);
  if (!l || l === "event") return null;
  const text = l === "pipeline" ? "Pipeline" : engagementLabel(r);
  return E("span", text, "chip lane lane-" + l);
}

function routeChip(r) {
  const x = jobRoute(r);
  if (!x || !x.cv) return null;
  return E(
    "span",
    `${x.cv} CV${x.via_agency ? " · via agency" : ""}`,
    "chip route",
  );
}

function reasonLine(r, n = 2) {
  const box = E("span", null, "reasons");
  rel(r)
    .reasons.filter((x) => x.weight || x.note)
    .sort((a, b) => !!b.note - !!a.note)
    .slice(0, n)
    .forEach((x) =>
      box.append(
        E(
          "span",
          (x.note ? "● " : x.weight > 0 ? "▲ " : "▼ ") + x.text,
          "reason " + (x.note ? "note" : x.weight > 0 ? "up" : "down"),
        ),
      ),
    );
  return box;
}

function stamp(s) {
  const d = new Date(s);
  return Number.isNaN(d.valueOf())
    ? String(s)
    : d.toLocaleString("en-GB", {
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
}

const isExcluded = (r) => !!r?.exclusion;

const jobRoute = (r) => r.route || rel(r).route;

export function cvSuggestionText(route, cvs = {}) {
  const cv = cvs[route?.suggested_cv], basis = route?.suggestion_basis;
  const source = {title_rule:'title', title_family:'title or recorded role'}[basis?.kind];
  if (!cv || !source || !basis?.term)
    return 'No title-based CV suggestion available.';
  return `Why suggested: ${cv.title || route.suggested_cv} — ${source} matches “${basis.term}”. Check this title-based suggestion against the role.`;
}

export { stateName, fieldName, fieldValue, relationName, urlLabel, chip, section, factualDate, statedDate, recordState, canonicalAction, commercialState, latestActivity, activityTime, contextLine, firstSentence, rel, relSort, bandChip, choiceLabel, hostOf, lane, engagementLabel, laneChip, routeChip, reasonLine, stamp, isExcluded, jobRoute };
