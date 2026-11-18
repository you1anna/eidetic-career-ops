import { createNavigation, PAGES as nav } from "./js/navigation.js";
import { createDecisionViews } from "./js/decisions.js";
import { createRecordDetail } from "./js/record-detail.js";
import { createRecordTimeline } from "./js/record-timeline.js";
import {
  stateName, chip, section, factualDate, recordState, canonicalAction, commercialState, latestActivity,
  contextLine, rel, relSort, bandChip, lane, laneChip, routeChip, reasonLine, isExcluded
} from "./js/record-presentation.js";
import { openIncomingRole as incomingForm } from "./js/incoming-role.js";
import { E, A, shortUrls, typeName } from "./js/dom.js";
import { api, onSignedOut } from "./js/api.js";
import { applySummaryDelta } from "./js/summary-delta.js";
import { decodeSummary } from "./js/summary-transport.js";
import { refreshEmailStatus } from "./js/email-status.js";
import { createEmailLeads } from "./js/email-leads.js";
import { refreshCaretakerStatus } from "./js/caretaker-status.js";
import { refreshResearchStatus } from "./js/research-status.js";
import { siteProblems, siteSummaryLine } from "./js/site-status.js";
import { createDashboardView, SERVER_SECTIONS, STATUS_SECTIONS } from "./js/dashboard.js";
import { statusReadDue } from "./js/status-refresh.js";
import { createFormHelpers, compact } from "./js/forms.js";
import { createOutreachViews } from "./js/outreach.js";
import { createProgressViews } from "./js/progress.js";
import { createWeekView, capacityText } from "./js/week.js";
import { createOpportunityCards } from "./js/opportunity-card.js";
import { summaryRefreshDecision, retainView } from "./js/summary-refresh.js";
import { createPeopleView } from "./js/people.js";
import { createSending } from "./js/sending.js";
import { createImportReview } from "./js/import-review.js";
import { createContactRouteLookup, checkLabel, refreshCheckLabels } from "./js/job-evidence.js";
import { suggestedContact } from "./js/approach-brief.js";

import { createRecordForms } from "./js/record-forms.js";
import { JOB_STATUS_NAMES } from "./js/labels.js";
const $ = (id) => document.getElementById(id);
let state = null,
  busy = false,
  statusReadAt = 0,
  statusReading = false,
  summaryReadAt = 0,
  summaryReading = false,
  summaryGeneration = 0,
  prioritiesPending = false,
  mutationsPending = 0,
  dashboardData = null,
  dashboardReadAt = 0,
  dashboardUpdatedAt = 0,
  dashboardError = null,
  dashboardReading = null,
  workFilter = "discovery",
  includeExcluded = false;
const navigation = createNavigation({
  getRecord: id => record(id), getDecisions: () => state.decisions || [],
  sheet: { isOpen: () => $("detail").open, close: () => $("detail").close(),
    showRecord: r => detail.showRecord(r), showDecision: (d, r) => detail.showDecision(d, r), showLead: id => emailLeads.show(id) },
  render, onTabChange: () => { $("search").value = ""; window.scrollTo(0, 0); render(); },
  seen, location, history, confirmDiscard: () => confirm("Discard unsaved changes?"),
});
const { label, selectField, recordFields } = createFormHelpers(() => {
  navigation.setDirty(true);
});
const emailLeads = createEmailLeads({navigation, load, getRecord:id=>record(id), showNotice,
  elements:{dialog:$("detail"),content:$("detail-content"),kind:$("detail-kind")}});
let recordsById = new Map();
const record = (id) => recordsById.get(id);
const contacts = createContactRouteLookup({ getState: () => state });
const progress = createProgressViews({ getState: () => state, getRecord: record, openRecord, openDecision, mutate,
  showNotice, setDirty: navigation.setDirty, revealAction: id => week.revealAction(id), openModal, openContactDetails });
const outreach = createOutreachViews({ getState: () => state, getRecord: record });
const cards = createOpportunityCards({getState: () => state, openRecord, mutate, canLeave, jobStatusSaved, showAlert});
const week = createWeekView({getState: () => state, getGroup: () => navigation.weekGroup, progress, openRecord, cards,
  renderDecisions: renderWeekDecisions, addIncoming: () => openIncoming(),
  openJobs: group => { workFilter = group; }});
const importReview = createImportReview({ getState: () => state, factualDate, openModal, mutate, showAlert });
const forms = createRecordForms({ getState: () => state, getRecord: record, openModal, label, selectField, recordFields, mutate, load,
    openRecord, showNotice, showAlert, setDirty: navigation.setDirty, recordState, canonicalAction });
const { openAddPerson, openCreate, openImport } = forms;
const decisions = createDecisionViews({ getState: () => state, getRecord: record, navigation, mutate, showNotice,
  isDetailOpen: () => $("detail").open });
const { decisionsIn, decisionButton } = decisions;
const timelines = createRecordTimeline({ getRecord: record });
const detail = createRecordDetail({ getState: () => state, getRecord: record, navigation, decisions, timelines,
  progress, outreach, forms, mutate, load, openIncoming, showNotice, showAlert, jobStatusSaved, statusCell,
  demotePerson, promotePerson, getContactIndex: contacts.index,
  elements: { content: $("detail-content"), kind: $("detail-kind"), dialog: $("detail") } });
const openSending = createSending({getState: () => state, getRecord: record, openModal,
  fetchTemplates: () => api('/api/message-templates'), mutate,
  refresh: async () => { if (!await load()) throw new Error('Could not refresh. Your wording is preserved. Try again when connected.'); },
  setDirty: navigation.setDirty});
const people = createPeopleView({ getState: () => state, getContactIndex: contacts.index, openRecord, openJob: navigation.openJob, recordProgress, addPerson: openAddPerson, openSending,
  importLinkedIn: openImport, importReview, demotePerson, promotePerson });
// Site problems from the loaded summary and run statuses only: the one-liners never fetch more.
const operationsDecisions = () => decisionsIn("open").filter(d => d.area === "operations" && !excludedDecision(d));
const problems = () => siteProblems(state, operationsDecisions());
const dashboard = createDashboardView({ emailLeads, getState: () => state, getData: () => dashboardData, getProblems: problems,
  getMeta: () => ({ loading: !!dashboardReading, error: dashboardError, updatedAt: dashboardUpdatedAt,
    retry: () => readDashboard(true, true) }),
  decisionsIn, decisionButton, excludedDecision: d => excludedDecision(d), go, openRecord,
  openWeek: navigation.openWeek, getContactIndex: contacts.index,
  openJobs: group => { workFilter = group; go("work"); } });
const subtitles = {
  dashboard: "What needs you, where the job hunt stands and how the site is running.",
  week: "Your priorities, the evidence behind them and what happens next.",
  people: "Who to contact, for which role, and what to do next.",
  work: "Discover, shortlist and track work through to its outcome.",
};
// Decisions: the assistant asks with declared answers; the owner answers here for later processing.
const excludedDecision = (d) => !!d.exclusion || isExcluded(record(d.record_id));
function showAlert(msg) {
  $("alert").textContent = msg;
  $("alert").hidden = !msg;
}
let noticeTimer;
function showNotice(msg, action = null) {
  clearTimeout(noticeTimer);
  // A modal sheet makes the page behind it inert; keep feedback and Undo in the active sheet.
  if ($("detail").open) $("detail").insertBefore($("notice"), $("detail-content"));
  else $("alert").after($("notice"));
  $("notice").replaceChildren(document.createTextNode(msg || ""));
  if (msg && action) {
    const actions = Array.isArray(action[0]) ? action : [action];
    actions.forEach(([label, run]) => {
      const b = E("button", label, "notice-action");
      b.type = "button";
      b.onclick = () => { $("notice").hidden = true; run(); };
      $("notice").append(" ", b);
    });
  }
  $("notice").hidden = !msg;
  if (msg)
    noticeTimer = setTimeout(() => {
      $("notice").hidden = true;
    }, action ? 15000 : 8000);
}
// Signing in from a new tab renews the cookie this page uses, so the page and anything typed in it
// survive; the prompt stays until it is used or replaced.
onSignedOut(() => {
  showNotice("Your sign-in has ended. Nothing can be saved until you sign in again.",
    ["Sign in in a new tab", () => window.open("/", "_blank", "noopener")]);
  clearTimeout(noticeTimer);
});
// With a summary from the server, ask only for what changed since its version. Any problem,
// including an older server without that route, falls back to the whole summary.
// `purpose` tells the server why the page is asking (save, background or manual), so the Dashboard's
// "refresh after a save" figure is not diluted by cheap background polls.
async function fetchSummary(base = state, purpose = "manual") {
  const headers = { "X-WO-Refresh": purpose };
  if (typeof base?.version === "string") {
    try {
      const update = decodeSummary(await api(`/api/state?view=summary&since=${encodeURIComponent(base.version)}&encoding=strings-v1`, { headers }));
      if (update.delta === true) return applySummaryDelta(base, update);
      if (typeof update.version === "string") return update;
    } catch (error) {
      if (error.signedOut) throw error;
    }
  }
  try {
    return decodeSummary(await api("/api/state?view=summary&encoding=strings-v1", { headers }));
  } catch (error) {
    // Navigation cancels fetches too: do not start a recovery request from a departing page.
    if (error.signedOut || error.cause instanceof TypeError || error.name === "AbortError") throw error;
    return api("/api/state?view=summary", { headers }); // older server or malformed transport: recover in full
  }
}
async function load(purpose = "manual") {
  while (busy) await new Promise((r) => setTimeout(r, 50));
  busy = true;
  summaryGeneration++;
  $("reload").disabled = true;
  $("connection-status").textContent = "Refreshing…";
  showAlert("");
  try {
    acceptSummary(await fetchSummary(state, purpose));
    render();
    routeRecord();
    return true;
  } catch (e) {
    showAlert(e.message);
    $("connection-status").textContent = e.signedOut ? "Signed out" : "Could not connect";
    return false;
  } finally {
    busy = false;
    $("reload").disabled = false;
  }
}
// Summary swaps are atomic; status-only reads stay independent of the record snapshot.
function acceptSummary(next) {
  state = next; summaryGeneration++; summaryReadAt = statusReadAt = Date.now(); prioritiesPending = false;
  timelines.clear();
  state.records ||= []; recordsById = new Map(state.records.map(r => [r.id, r]));
  for (const name of ['links', 'interactions', 'history', 'review_queue']) state[name] ||= [];
  state.coverage ||= {channels: [], runs: []};
  $("connection-status").textContent = state.mode === "read-only" ? "Read-only workspace" : "Workspace connected";
  $("loaded").textContent = state.loaded ? `Loaded ${factualDate(state.loaded)}` : `${state.records.length} records`;
  renderPriorityUpdate();
}
function renderPriorityUpdate() {
  let button = $("priority-update");
  if (!button) {
    button = E('button', 'New priorities available · Refresh', 'priority-update'); button.id = 'priority-update'; button.type = 'button';
    button.onclick = async () => { if (canLeave()) await load(); };
    $('alert').before(button);
  }
  button.hidden = !prioritiesPending;
}
async function readPriorities(returning = false) {
  if (!state || busy || summaryReading || !statusReadDue(summaryReadAt, Date.now(), {returning, hidden: document.hidden})) return;
  summaryReading = true;
  const base = state, generation = summaryGeneration;
  try {
    const next = await fetchSummary(base, "background");
    if (generation !== summaryGeneration || busy) return;
    summaryReadAt = Date.now();
    $('connection-status').textContent = state.mode === 'read-only' ? 'Read-only workspace' : 'Workspace connected';
    const decision = summaryRefreshDecision({baseVersion: base.version, currentVersion: state.version,
      nextVersion: next.version, editing: mutationsPending > 0 || navigation.dirty || $('detail').open || $('modal').open});
    if (decision === 'pending') { prioritiesPending = true; renderPriorityUpdate(); }
    if (decision === 'apply') {
      const restore = retainView($('content'));
      acceptSummary(next); render(); restore($('content'));
    }
  } catch (error) {
    // A failed background read keeps the current priorities, edits and sign-in recovery intact.
    summaryReadAt = Date.now();
    if (!error.signedOut) $('connection-status').textContent = 'Could not refresh priorities · showing saved view';
  } finally { summaryReading = false; }
}
function renderNav() {
  const n = $("nav");
  n.replaceChildren();
  const toDo = (state.opportunities?.act_now?.length || 0)
    + decisionsIn("open").filter(d => d.kind !== "draft" && d.area !== "operations" && !excludedDecision(d)).length;
  const activeJobs = (state.records || []).filter((r) => r.type === "job" && ["pursue", "applied", "responded", "interview", "offer"].includes(r.status)).length;
  const counts = { week: toDo || "", people: people.count() || "", work: activeJobs || "", dashboard: problems().problems.length || "" };
  nav.forEach(([key, label]) => {
    const active = key === navigation.tab;
    const b = E("button", null, active ? "active" : "");
    b.type = "button";
    b.dataset.tab = key;
    if (active) b.setAttribute("aria-current", "page");
    const count = counts[key];
    b.append(E("span", label), E("span", count, "nav-count"));
    b.onclick = () => go(key);
    n.append(b);
  });
}
function render() {
  if (!state) return;
  renderNav();
  $("title").textContent = nav.find((x) => x[0] === navigation.tab)?.[1] || "This week";
  $("subtitle").textContent = subtitles[navigation.tab] || "";
  $("page-actions").replaceChildren();
  const c = $("content");
  c.replaceChildren();
  const q = $("search").value.trim().toLowerCase();
  if (q) return renderSearch(c, q);
  if (navigation.tab === "week") { renderWeek(c); if (navigation.weekGroup) week.revealSection(navigation.weekGroup); }
  if (navigation.tab === "people") people.render(c);
  if (navigation.tab === "work") renderWork(c);
  if (navigation.tab === "dashboard") { dashboard.render(c); readDashboard(true); }
}
function renderWeekDecisions(c) {
  const commercial = decisionsIn("open").filter(d => d.kind !== "draft" && d.area !== "operations" && !excludedDecision(d));
  if (!commercial.length) return;
  const box = E("section", null, "week-section week-decisions");
  box.dataset.weekGroup = "decisions";
  box.append(section("Decisions that need your judgment", commercial.length));
  const list = E("div", null, "group-list");
  commercial.slice(0, 2).forEach(d => list.append(decisionButton(d))); box.append(list);
  if (commercial.length > 2) {
    const rest = A(E("details", null, "lower"), E("summary", `More decisions (${commercial.length - 2})`));
    let built = false;
    rest.addEventListener("toggle", () => {
      if (rest.open && !built) { built = true; commercial.slice(2).forEach(d => rest.append(decisionButton(d))); }
    }); box.append(rest);
  }
  c.append(box);
}
function opportunityEntries(group) {
  const all = new Map([...(state.intelligence?.actions || []), ...(state.intelligence?.research || [])].map(a => [a.id, a]));
  return (state.opportunities?.[group] || []).map(ref => ({ ref, record: record(ref.record_id), item: all.get(ref.action_id) })).filter(x => x.record);
}
function opportunityList(entries, group, limit = 10) {
  const list = E('div', null, 'opportunity-list approach-list');
  let shown = 0;
  const more = E('button', 'Show more', 'show-more'); more.type = 'button';
  const show = () => {
    const end = Math.min(entries.length, shown ? shown + 10 : limit);
    for (; shown < end; shown++) {
      const {ref, record:r, item} = entries[shown];
      const row = item?.progress && item.record_id === ref.record_id && ['active','shortlisted','closed'].includes(group) ? progress.attentionCard(item) : jobRow(r);
      if (r.type === 'job' && ['active','shortlisted','closed'].includes(group) && item?.progress) row.querySelector('.approach-top')?.after(checkLabel(r.evidence_checks?.vacancy, 'vacancy', r));
      row.dataset.opportunityId = ref.record_id; row.dataset.opportunityLane = ref.lane;
      list.append(row);
    }
    more.hidden = shown >= entries.length; more.textContent = `Show more (${entries.length - shown} remaining)`;
  };
  more.onclick = show; show();
  return A(E('div'), list, more);
}
function renderWeek(c) {
  week.render(c);
  if (state.relevance_error) showAlert(`Ranking unavailable: ${state.relevance_error}`);
  if (state.review_queue.length) {
    const review = E("button", `Review imported people (${state.review_queue.length})`); review.type = "button";
    review.onclick = () => { go("people"); $("import-review")?.scrollIntoView(); }; c.append(review);
  }
  c.append(siteSummaryLine(problems()));
}
// A newer read of the run statuses redraws only their blocks; the rest of the page and its forms stay.
async function readRunStatus(returning) {
  if (!state || busy || statusReading || !statusReadDue(statusReadAt, Date.now(), { returning, hidden: document.hidden })) return;
  statusReading = true;
  const started = Date.now(), generation = summaryGeneration;
  try {
    const update = await api("/api/status");
    if (generation !== summaryGeneration || statusReadAt > started) return;
    Object.assign(state, update);
    statusReadAt = Date.now();
    redrawStatus(STATUS_SECTIONS);
  } catch {
    // Unread, the labels keep ageing: an old run still shows as overdue. A sign-in ending is shown by api().
  } finally {
    statusReading = false;
  }
}
// The status blocks are the one-liners and the Dashboard; neither holds a form, so redrawing keeps no input.
// Only the Dashboard sections fed by what changed are rebuilt (server figures or run statuses).
function redrawStatus(sections) {
  for (const line of document.querySelectorAll("#content .site-line")) line.replaceWith(siteSummaryLine(problems()));
  const board = document.querySelector("#content .dashboard");
  if (board) dashboard.refresh(board, sections);
  renderNav();
}
// Server-only Dashboard figures, read only while that tab is open: on opening it, on return and
// every five visible minutes, or at once from Retry. One read at a time; leaving the tab abandons
// the reply. A failure is shown as such: the last good figures stay, labelled with their time.
async function readDashboard(opening = false, force = false) {
  if (!state || navigation.tab !== "dashboard" || dashboardReading) return;
  if (!force && !opening && !statusReadDue(dashboardReadAt, Date.now(), { hidden: document.hidden })) return;
  if (!force && opening && dashboardData && Date.now() - dashboardReadAt < 30000) return;
  const controller = new AbortController();
  dashboardReading = controller;
  const leave = () => { if (navigation.tab !== "dashboard") controller.abort(); };
  window.addEventListener("hashchange", leave);
  if (force || !dashboardData) redrawStatus(["figures"]);  // show "Loading…" or the retry in progress
  try {
    const next = await api("/api/dashboard", { signal: controller.signal });
    if (navigation.tab !== "dashboard") return;
    dashboardData = next;
    dashboardUpdatedAt = Date.now();
    dashboardError = null;
  } catch (error) {
    if (controller.signal.aborted) return;  // the owner left the tab; nothing failed
    dashboardError = error.message || "the request failed";
  } finally {
    dashboardReadAt = Date.now();  // a failed attempt also waits for the next cadence, not every minute
    window.removeEventListener("hashchange", leave);
    dashboardReading = null;
    if (navigation.tab === "dashboard") redrawStatus(SERVER_SECTIONS);
  }
}
function renderWork(c) {
  c.append(siteSummaryLine(problems()));
  const actions = E('div', null, 'toolbar work-actions');
  const save = E('button', 'Save to shortlist', 'primary'); save.type='button'; save.onclick=openSaveJob;
  const incoming = E('button', 'Add incoming role'); incoming.type='button'; incoming.onclick=()=>openIncoming();
  actions.append(save, incoming); c.append(actions);
  if (!state.opportunities || state.intelligence_error) {
    c.append(E('p', 'Work selection is unavailable. Refresh to try again; records remain searchable.', 'alert')); return;
  }
  const filters = E('div', null, 'toolbar work-filters'); filters.setAttribute('aria-label', 'Work progress');
  const filter = (key, name, count = true) => {
    const entries = opportunityEntries(key), b = E('button', count ? `${name} (${entries.length})` : name, workFilter === key ? 'active' : '');
    b.type='button'; b.dataset.workFilter=key; b.setAttribute('aria-pressed', String(workFilter===key));
    b.onclick=()=>{workFilter=key; render();}; return b;
  };
  for (const [key, name] of [['discovery','Discovery'],['shortlisted','Shortlisted'],['active','Active']]) filters.append(filter(key, name));
  c.append(filters);
  const history = A(E('details', null, 'work-history'), E('summary', 'History and reference'));
  history.open = ['other','closed'].includes(workFilter);
  history.append(A(E('div', null, 'toolbar'), filter('closed', 'Closed / skipped', false), filter('other', 'Reference', false)));
  c.append(history);
  if (state.discovery_capacity) c.append(E('p', capacityText(state.discovery_capacity), 'muted'));
  if (workFilter === 'other') c.append(E('p', 'No review required. Search finds all records. Opportunities return to the working list when the evidence earns them a place.', 'muted'));
  const entries=opportunityEntries(workFilter);
  c.append(entries.length ? opportunityList(entries, workFilter) : E('p','No work in this group.', 'empty'));
}
function openIncoming(record) {
  incomingForm({ record, label, openModal, saved: async result => {
    await load("save");
    go("work");
    openRecord(result.record.id);
    showNotice("Hiring process saved. Its original history is retained.");
  }});
}
function openSaveJob() {
  const fields = E("div", null, "form-grid");
  fields.append(
    label("Source link", "url", "", "url", true, true),
    label("Job title", "name", "", "text", false, true),
    label("Employer (if known)", "organisation"),
    label("Why interested (optional, up to 4,000 characters)", "summary", "", "textarea", true),
    label("Pasted advert details (optional, up to 20,000 characters)", "notes", "", "textarea", true),
    E("p", "Save what you know. Opening an advert or CV does not record an application.", "muted span-2"),
  );
  openModal("Save to shortlist", fields, async data => {
    if (data.summary.length > 4000) throw new Error("Why interested is over 4,000 characters. Shorten it before saving; your text has been kept.");
    if (data.notes.length > 20000) throw new Error("Pasted advert details are over 20,000 characters. Shorten them before saving; your text has been kept.");
    const result = await api("/api/jobs/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(compact(data)) });
    workFilter = "shortlisted";
    await load("save");
    openRecord(result.record.id);
    const message = result.created ? "Added to shortlist." : "Already saved. Opened the existing record with its status unchanged.";
    const confirmation = E("p", message, "notice save-confirmation");
    confirmation.setAttribute("role", "status");
    $("detail-content").prepend(confirmation);
    showNotice(message);
  }, "Save to shortlist");
}
function warmContactsFor(r) {
  if (!["job", "opportunity", "company", "agency"].includes(r.type)) return [];
  return contacts.forRecord(r).map(route => route.person);
}
function table(items, repeats = null) {
  if (!items.length) return E("p", "No matching records.", "empty");
  if (items.every(r => r.type === "job")) return jobList(items, repeats);
  const t = E("div", null, "table"),
    head = E("div", null, "table-head");
  [
    "Name / organisation",
    "Why it ranks here",
    "Status",
    "Last activity",
    "Context / next step",
  ].forEach((x) => head.append(E("span", x)));
  t.append(head);
  items.forEach((r) => {
    if (r.type === "job") { t.append(jobRow(r, repeats?.get(r.id))); return; }
    const p = recordProgress(r),
      warm = warmContactsFor(r),
      b = E("button", null, "table-row"),
      context = contextLine(r),
      action = p ? p.next : canonicalAction(r),
      contextCell = A(E("span", null, "stack"), E("span", context));
    if (warm.length && r.type !== "person")
      contextCell.prepend(E("small", `Contacts: ${warm.map((w) => w.name).slice(0, 2).join(", ")}`, "warm-badge"));
    if (r.type === "job") contextCell.prepend(checkLabel(r.evidence_checks?.vacancy, 'vacancy', r));
    if (action && action.trim().toLowerCase() !== context.trim().toLowerCase())
      contextCell.append(E("small", `Next: ${shortUrls(action)}`));
    const rowWrap = E("div", null, "table-row-wrap");
    b.type = "button";
    b.onclick = () => openRecord(r.id);
    b.append(
      A(
        E("span"),
        E("strong", r.name || "Untitled"),
        isExcluded(r) ? chip("Excluded", "blocked") : null,
        E(
          "small",
          [
            typeName(r.type),
            r.organisation || r.role || r.linkedin?.observed_role || (r.linkedin?.observed_company ? `${r.linkedin.observed_company} (LinkedIn)` : ""),
            r.type === "job" ? JOB_STATUS_NAMES[r.status] : commercialState(r) && stateName(commercialState(r)),
          ]
            .filter(Boolean)
            .join(" · "),
        ),
      ),
      A(
        E("span", null, "rank-cell"),
        A(E("span", null, "chips"), laneChip(r), routeChip(r), bandChip(r),
          warm.length && r.type === "job" ? chip(`${warm.length} contact${warm.length === 1 ? '' : 's'}`) : null),
        reasonLine(r, 1),
      ),
      statusCell(r, p),
      E("span", factualDate(latestActivity(r)), "muted"),
      contextCell,
    );
    rowWrap.append(b);
    t.append(rowWrap);
  });
  return t;
}
function jobList(items, repeats = null) {
  const list = E("div", null, "table job-brief-list");
  items.forEach(r => list.append(jobRow(r, repeats?.get(r.id))));
  return list;
}
function jobRow(r, repeated = 0) {
  const row = E("div", null, "table-row-wrap");
  const open = E("button", null, "table-row job-brief-row"); open.type = "button";
  open.onclick = () => openRecord(r.id);
  const route = suggestedContact(r, state, isExcluded, contacts.index());
  open.append(A(E("span", null, "stack"), E("strong", r.name),
    E("small", [r.organisation, r.pay, r.location].filter(Boolean).join(" · ")),
    reasonLine(r, 1), route ? E("small", `Contact: ${route.person.name}`) : null,
    repeated ? E("small", `Also listed ${repeated} more time${repeated === 1 ? "" : "s"}; see Show more`, "muted") : null),
    A(E("span", null, "stack"), laneChip(r), r.type === "job" ? checkLabel(r.evidence_checks?.vacancy, "vacancy", r) : null,
      isExcluded(r) ? chip("Excluded", "blocked") : r.status === "candidate" && !recordProgress(r) ? null : statusCell(r),
      E("span", "Open brief →", "job-open-brief")));
  row.append(open);
  return row;
}
// A job's status button can be a mistaken tap, and Rejected and Closed have no way back, so the notice
// offers Undo, as the approach cards do: the status returns to what it was and the change stays in the history.
function jobStatusSaved(id, to) {
  showNotice(`Application status: ${JOB_STATUS_NAMES[to] || typeName(to)}.`, ["Undo", async () => {
    try {
      await mutate(`/api/records/${encodeURIComponent(id)}/correct`, { revision: record(id)?.revision, field: "status",
        note: "Undone straight after saving" });
      if (navigation.tab === 'week' && !$('detail').open) week.revealRecord(id);
      showNotice(`Undone: the application status is back to ${JOB_STATUS_NAMES[record(id)?.status] || "what it was"}.`);
    } catch (error) { showAlert(error.message); }
  }]);
  if (navigation.tab === 'week' && !$('detail').open) $('notice').querySelector('button')?.focus({preventScroll:true});
}
async function demotePerson(r) {
  if (!r || !canLeave()) return;
  const current = record(r.id) || r;
  try {
    await mutate(`/api/records/${encodeURIComponent(current.id)}/transition`, {
      field: "relationship", to: "do_not_contact", revision: current.revision, note: "Moved to Not contacting"
    });
    showNotice(`Moved ${current.name} to Not contacting.`, ["Undo", async () => {
      try {
        const latest = record(current.id);
        await mutate(`/api/records/${encodeURIComponent(current.id)}/correct`, {
          revision: latest?.revision, field: "relationship", note: "Undone straight after demoting"
        });
        showNotice(`Undone: ${current.name} is back in your contacts.`);
      } catch (error) { showAlert(error.message); }
    }]);
  } catch (error) { showAlert(error.message); }
}
async function promotePerson(r, { relationship = "dormant", priority = null } = {}) {
  if (!r || !canLeave()) return;
  const current = record(r.id) || r;
  const wasDemoted = current.relationship === "do_not_contact";
  const prevRelationship = current.relationship || "none";
  const prevPriority = current.priority || null;
  const url = `/api/records/${encodeURIComponent(current.id)}`;
  try {
    let rev = current.revision;
    if (wasDemoted || (relationship && current.relationship !== relationship)) {
      const res = await mutate(`${url}/transition`, {
        field: "relationship", to: relationship, revision: rev, note: "Promoted to current contacts"
      });
      rev = res.record.revision;
    }
    if ((record(current.id)?.priority || null) !== (priority || null)) {
      await mutate(url, { priority: priority || null, revision: rev }, "PATCH");
    }
    const targetLabel = priority === "first" ? "First priority" : priority === "next" ? "Next" : "current contacts";
    showNotice(`Promoted ${current.name} to ${targetLabel}.`, ["Undo", async () => {
      try {
        const latest = record(current.id);
        let undoRev = latest?.revision;
        if (wasDemoted) {
          await mutate(`${url}/transition`, {
            field: "relationship", to: "do_not_contact", revision: undoRev, note: "Undone straight after promoting"
          });
          showNotice(`Undone: ${current.name} is back in Not contacting.`);
        } else {
          if ((latest?.priority || null) !== prevPriority) {
            const res = await mutate(url, { priority: prevPriority, revision: undoRev }, "PATCH");
            undoRev = res.record.revision;
          }
          if (latest?.relationship !== prevRelationship) {
            await mutate(`${url}/correct`, {
              revision: undoRev, field: "relationship", note: "Undone straight after promoting"
            });
          }
          showNotice(`Undone: ${current.name}'s list placement was restored.`);
        }
      } catch (error) { showAlert(error.message); }
    }]);
  } catch (error) { showAlert(error.message); }
}
// One status wherever a record appears: its approach's progress, else its application status.
function recordProgress(r) {
  const items = isExcluded(r) ? [] : outreach.forRecord(r);
  const shown = items.find((a) => a.progress?.section === "in_progress") || items.find((a) => a.progress && a.progress.section !== "research");
  return shown?.progress || null;
}
function statusCell(r, p = recordProgress(r)) {
  if (r.type === 'person' && p && !p.history?.length) return E('span', 'No contact outcome recorded', 'muted');
  if (p) return chip(p.label, p.status === "follow_up" ? "ready" : "");
  if (r.type === "job" && r.status) return E("span", JOB_STATUS_NAMES[r.status] || typeName(r.status), "muted");
  const s = recordState(r);
  return s === "needs_review" ? E("span", "—", "muted state-quiet") : chip(stateName(s), s);
}
function renderSearch(c, q) {
  $("title").textContent = "Search";
  $("subtitle").textContent = `Results across all record types`;
  const rs = state.records
    .filter((r) =>
      Object.values(r).some(
        (v) => typeof v === "string" && v.toLowerCase().includes(q),
      ) ||
      (r.linkedin?.observed_company || "").toLowerCase().includes(q) ||
      (r.linkedin?.observed_role || "").toLowerCase().includes(q),
    )
    .sort(relSort);
  c.append(section("Records", rs.length), table(rs));
}
// The owner's list on a person's sheet: his order, how well he knows them, who they are, how he reaches
// them and what he asks. Every option is one the API accepts (states.py vocabularies and transitions).
function openModal(title, fields, submit, labelText = "Save", options = {}) {
  const previousDirty = navigation.dirty;
  let committed = false;
  $("modal").addEventListener("close", () => {
    if (!committed) navigation.setDirty(previousDirty);
  }, { once: true });
  navigation.setDirty(false);
  $("modal-title").textContent = title;
  $("modal-body").replaceChildren(fields);
  $("modal-form").querySelector("[type=submit]").textContent = labelText;
  $("modal-status").textContent = "";
  $("modal-form").onsubmit = async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    if (form.getAttribute('aria-busy') === 'true') return;
    const data = Object.fromEntries(new FormData(form));
    const controls = [...form.querySelectorAll('button, input, select, textarea')].map(node => [node, node.disabled]);
    form.setAttribute('aria-busy', 'true');
    controls.forEach(([node]) => { node.disabled = true; });
    navigation.setDirty(false);
    try {
      if (await submit(data) === false) {
        if (!options.managesDirty) navigation.setDirty(true);
        return;
      }
      committed = true;
      $("modal").close();
    } catch (err) {
      navigation.setDirty(true);
      $("modal-status").textContent = err.message;
    } finally {
      form.removeAttribute('aria-busy');
      controls.forEach(([node, disabled]) => { node.disabled = disabled; });
      options.afterSubmit?.();
    }
  };
  $("modal").showModal();
  fields.querySelector("input,select,textarea")?.focus();
}
async function mutate(path, payload, method = "POST") {
  mutationsPending++; summaryGeneration++;
  try {
  const result = await api(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const editingTab = navigation.tab;
  const keepEdits = ["week", "work"].includes(editingTab) ? progress.retainEdits(decodeURIComponent(path.match(/^\/api\/records\/([^/]+)/)?.[1] || "")) : [];
  timelines.clear();
  await load("save");
  if (navigation.tab === editingTab) progress.restoreEdits(keepEdits);
  if (navigation.recordId && record(navigation.recordId)) detail.render(record(navigation.recordId));
  else if (navigation.decisionId) {
    const d = (state.decisions || []).find((x) => x.id === navigation.decisionId);
    if (d) openDecision(d);
  }
  if (progress.hasEdits()) navigation.setDirty(true);
  return result;
  } finally { mutationsPending--; }
}
$("detail").addEventListener("close", () => $("alert").after($("notice")));
// Counts which section is used (the server logs the section, never the record); a failure is ignored.
let lastSeen = "";
function seen(view) {
  if (view === lastSeen) return;
  lastSeen = view;
  fetch(`/api/seen/${view}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }).catch(() => {});
}
$("reload").onclick = () => { if (canLeave()) load(); };
$("new-record").onclick = () => openCreate();
$("close").onclick = closeDetail;
$("detail").addEventListener("cancel", (e) => {
  if (!navigation.cancelDetail()) e.preventDefault();
});
$("modal-close").onclick = $("modal-cancel").onclick = () => {
  if ($("modal-form").getAttribute('aria-busy') !== 'true' && canLeave()) $("modal").close();
};
$("modal").addEventListener("cancel", (e) => {
  if ($("modal-form").getAttribute('aria-busy') === 'true' || !canLeave()) e.preventDefault();
});
$("search").oninput = render;
document.addEventListener("keydown", (e) => {
  if (
    e.key === "/" &&
    !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)
  ) {
    e.preventDefault();
    $("search").focus();
  }
  if ((e.metaKey || e.ctrlKey) && e.key === "k") {
    e.preventDefault();
    $("search").focus();
  }
});
window.addEventListener("beforeunload", (e) => {
  if (navigation.dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
window.addEventListener("hashchange", () => {
  if (state) routeRecord();
});
// Re-read status and priorities on return or every five visible minutes. Edited snapshots stay put.
const tick = (returning) => { refreshCheckLabels(); refreshEmailStatus(); refreshResearchStatus(); refreshCaretakerStatus(); readRunStatus(returning); readPriorities(returning); readDashboard(returning); };
setInterval(() => tick(false), 60000);
window.addEventListener('focus', () => tick(true));
document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(true); });
load();

function openRecord(...args) { return navigation.openRecord(...args); }
function openDecision(...args) { return navigation.openDecision(...args); }
function go(key) { return navigation.go(key); }
function canLeave() { return navigation.canLeave(); }
function closeDetail() { return navigation.closeDetail(); }
function routeRecord() { return navigation.routeRecord(); }
function openContactDetails(...args) { return detail.openContactDetails(...args); }
