import {jobStatusButton} from './job-actions.js';
// Record sheets and inline controls. Routing, decisions and timeline requests have separate owners.
import { E, A, L, extLink, linkify, typeName } from "./dom.js";
import { api } from "./api.js";
import { incomingSummary } from "./incoming-role.js";
import { personRouteEvidence, vacancyEvidence } from "./job-evidence.js";
import { agencyApproachBrief, jobApproachBrief, personApproachBrief, workAssessment } from "./approach-brief.js";
import { peopleContext } from "./people-context.js";
import {
  JOB_STATUS_NAMES, RELATIONSHIP_NAMES, KIND_NAMES, PRIORITY_NAMES, CHANNEL_NAMES,
  ASK_NAMES
} from "./labels.js";
import {
  fieldName, fieldValue, relationName, urlLabel, section, factualDate, rel, bandChip, choiceLabel,
  laneChip, routeChip, reasonLine, isExcluded, jobRoute, engagementLabel, cvSuggestionText
} from "./record-presentation.js";

export function createRecordDetail({ getState, getRecord: record, navigation, decisions, timelines,
  progress, outreach, forms, mutate, load, openIncoming, showNotice, showAlert, jobStatusSaved, statusCell,
  demotePerson, promotePerson, getContactIndex,
  elements: { content, kind, dialog } }) {
  const { decisionsIn, decisionFor, decisionButton, decisionPanel } = decisions;
  const { openEdit, openTransition, openFollowup, openInteraction, openLink } = forms;
  const { openRecord, openJob, canLeave } = navigation;
  function related(id) {
    const state = getState();
    const ids = [];
    (state.links || []).forEach((l) => {
      if (l.from_id === id) ids.push([l.to_id, l]);
      if (l.to_id === id) ids.push([l.from_id, l]);
    });
    return ids.map(([x, l]) => [record(x), l]).filter((x) => x[0]);
  }

  function disclosure(title, content, cls = "") {
    return A(E("details", null, `detail-disclosure ${cls}`), E("summary", title), content);
  }

  function openContactDetails(id, field, actionId) {
    // Update the route without a hashchange render that would fold the field away again.
    openRecord(id, false, actionId);
    if (navigation.recordId !== id) return;
    navigation.keepRoute();
    const input = content.querySelector(`.list-panel [name="${field}"]`);
    if (!input) return;
    for (let parent = input.parentElement; parent; parent = parent.parentElement)
      if (parent.tagName === "DETAILS") parent.open = true;
    input.focus();
    input.scrollIntoView({ block: "center" });
  }

  function jobControls(r) {
    const state = getState();
    const box = E("section", null, "job-controls");
    box.setAttribute("aria-label", "Job and application controls");
    const links = E("div", null, "job-links");
    const advert = r.detail_url || r.url;
    const application = !isExcluded(r) && r.apply_url;
    if (application || advert) links.append(extLink(application || advert, application ? "Open application ↗" : "Open advert ↗"));
    else links.append(E("span", "Advert link not recorded", "muted"));
    const route = jobRoute(r);
    if (r.source_context?.basis === 'email') {
      const original = E('button', 'Review email context'); original.type = 'button';
      original.onclick = async () => {
        if (!navigation.canLeave()) return;
        try {
          const lead = await api(`/api/records/${encodeURIComponent(r.id)}/email-lead`);
          navigation.openLead(lead.id);
        } catch (error) { showAlert(error.message); }
      };
      links.append(original);
    }
    box.append(links);
    const cvSection = E("section", null, "cv-choice");
    cvSection.setAttribute("aria-label", "CV choice");
    const cvLinks = E("div", null, "job-links");
    if (route?.url) cvLinks.append(extLink(route.url, "Open CV ↗"));
    else cvLinks.append(E("span", "CV link not available yet.", "muted"));
    if (!isExcluded(r) && Object.keys(state.cvs || {}).length) {
      const form = E("form", null, "cv-override"), field = E("label", null, "field");
      field.append(E("span", "CV for this job"));
      const select = E("select"); select.name = "cv_override";
      const suggestion = E("option", "Use suggestion"); suggestion.value = ""; select.append(suggestion);
      Object.entries(state.cvs).forEach(([key, cv]) => {
        const option = E("option", `${cv.title || key} · ${typeName(cv.readiness || "draft")}`);
        option.value = key; select.append(option);
      });
      const savedChoice = r.cv_override || "";
      select.value = savedChoice;
      field.append(select);
      const save = E("button", "Save CV choice"); save.type = "submit";
      const status = E("p", "", "muted"); status.setAttribute("role", "status");
      select.onchange = () => {
        navigation.setDirty(true);
        const changed = select.value !== savedChoice;
        cvLinks.hidden = changed;
        status.textContent = changed ? "Save your choice to update the CV link." : "";
      };
      form.append(field, save, status);
      form.onsubmit = async event => {
        event.preventDefault();
        if (form.getAttribute('aria-busy') === 'true') return;
        const choice = select.value;
        form.setAttribute('aria-busy', 'true');
        select.disabled = save.disabled = true; navigation.setDirty(false);
        try {
          await mutate(`/api/records/${encodeURIComponent(r.id)}`, { revision: r.revision, cv_override: choice || null }, "PATCH");
          showNotice(choice ? "CV choice saved for this job." : "Suggested CV restored.");
        } catch (error) { navigation.setDirty(true); status.textContent = error.message; }
        finally { form.removeAttribute('aria-busy'); select.disabled = save.disabled = false; }
      };
      cvSection.append(form);
    }
    if (route?.cv) cvSection.append(E("p", `${route.title || route.cv + " CV"} · ${typeName(route.readiness || "draft")} · ${route.selection === "manual" ? "Your selection" : "Suggested"}${route.via_agency ? " · via agency" : ""}${route.readiness === "draft" ? " · Review this draft before use." : ""}`, "cv-status"));
    if (!isExcluded(r)) cvSection.append(E("p", cvSuggestionText(route, state.cvs), "cv-suggestion muted"));
    cvSection.append(cvLinks);
    box.append(cvSection);
    box.append(E("p", `Application status: ${JOB_STATUS_NAMES[r.status] || "Not recorded"}`, "application-status"));
    const actions = E("div", null, "job-status-actions");
    const more = A(E("details", null, "application-options"), E("summary", "More application options"));
    const nextStatus = ({ candidate: "applied", pursue: "applied", applied: "responded", responded: "interview", interview: "offer" })[r.status];
    let nextButton = null;
    (r.transitions?.status || []).filter(to => !isExcluded(r) || !["pursue", "applied"].includes(to)).forEach(to => {
      const button = jobStatusButton(r, to, {mutate, canLeave, saved: jobStatusSaved, failed: showAlert});
      if (to === nextStatus) nextButton = button;
      else actions.append(button);
    });
    if (nextButton) box.append(nextButton);
    more.append(actions);
    box.append(more);
    box.append(E("p", isExcluded(r) ? "Excluded from new applications. Existing activity can still be recorded." : "You submit applications yourself. Opening a link leaves the status unchanged.", "muted job-hint"));
    return box;
  }

  function personListControls(r) {
    if (!demotePerson && !promotePerson) return null;
    const bar = E("div", null, "person-list-controls");
    if (r.relationship === "do_not_contact") {
      const promote = E("button", "Promote to current contacts", "primary");
      promote.type = "button";
      promote.dataset.personAction = "promote";
      promote.onclick = () => promotePerson?.(r, { relationship: "dormant", priority: null });
      const first = E("button", "Promote to First priority");
      first.type = "button";
      first.dataset.personAction = "promote-first";
      first.onclick = () => promotePerson?.(r, { relationship: "dormant", priority: "first" });
      const next = E("button", "Promote to Next");
      next.type = "button";
      next.dataset.personAction = "promote-next";
      next.onclick = () => promotePerson?.(r, { relationship: "dormant", priority: "next" });
      bar.append(promote, first, next);
      return bar;
    }
    const isCurrent = Boolean(r.priority || ["warm", "active", "dormant"].includes(r.relationship));
    if (!isCurrent && promotePerson) {
      const promote = E("button", "Promote to current contacts");
      promote.type = "button";
      promote.dataset.personAction = "promote";
      promote.onclick = () => promotePerson(r, { relationship: "dormant", priority: null });
      bar.append(promote);
    }
    if (demotePerson) {
      const demote = E("button", "Demote to Not contacting");
      demote.type = "button";
      demote.dataset.personAction = "demote";
      demote.onclick = () => demotePerson(r);
      bar.append(A(E('details',null,'person-more'),E('summary','More'),demote));
    }
    return bar;
  }

  function renderDetail(r) {
    const state = getState();
    navigation.setDirty(false);
    const c = content;
    c.replaceChildren();
    const top = E("div", null, "detail-title");
    const h = E("h2", r.name || "Untitled");
    h.id = "detail-title";
    top.append(
      h,
      A(
        E("span", null, "chips"),
        laneChip(r),
        routeChip(r),
        statusCell(r),
      ),
    );
    if (navigation.backTarget) {
      const [backId, backAction] = navigation.backTarget;
      const back = E("button", `← Back to ${record(backId)?.name || "the previous record"}`, "linkish back-link");
      back.type = "button";
      back.onclick = () => navigation.goBack();
      c.append(back);
    }
    c.append(top);
    if (r.type === 'job') c.append(E('p', r.organisation || 'Company not stated', 'job-company'));
    const focused = ["person", "job", "agency"].includes(r.type);
    const supporting = focused ? E("div", null, "brief-supporting-content") : c;
    const approaches = isExcluded(r) ? [] : outreach.forRecord(r);
    const personContext = r.type === 'person' ? peopleContext(state, new Date(), getContactIndex?.()).byId.get(r.id) : null;
    const current = approaches.find((a) => a.id === navigation.actionId) || personContext?.item || approaches.find((a) => a.progress?.section === "in_progress") || approaches[0];
    const incoming = r.type === "job" ? incomingSummary(r, openIncoming, async action => {
      try { await api(action.url,{method:action.method,headers:{"Content-Type":"application/json"},body:JSON.stringify(action.payload)});
        await load("save"); openRecord(r.id); showNotice("Stage reverted. Private evidence and history are retained.");
      } catch(error) { showNotice(error.message); }
    }) : null;
    if (r.type === "job") c.append(jobApproachBrief(r, state, { progress, current, openRecord, jobControls, isExcluded, showNotice, incoming, routeIndex: getContactIndex?.() }));
    else if (r.type === "person") {
      c.append(personApproachBrief(r, state, { progress, current, openRecord, openJob, isExcluded, showNotice, personListControls, context:personContext }));
      supporting.append(personRouteEvidence(r, state));
    }
    else if (r.type === "agency") c.append(agencyApproachBrief(r, state, { progress, current, openRecord, openJob, isExcluded, showNotice }));
    else { c.append(workAssessment(r,current)); if(current)c.append(progress.block(current)); }
    if (r.type === "job" && current) supporting.append(disclosure("Approach progress", progress.block(current)));
    const otherApproaches = approaches.filter((a) => a !== current && a.progress);
    if (otherApproaches.length) {
      // A person can be an approach at more than one company; each keeps its own status and history.
      const box = A(E("div", null, "other-approaches"), E("p", "Other approaches for this person", "muted"));
      otherApproaches.forEach((a) => {
        const b = E("button", `${a.company || a.title} · ${a.progress.label}`, "linkish");
        b.type = "button"; b.dataset.otherApproach = a.id;
        b.onclick = () => openRecord(r.id, true, a.id, true);
        box.append(A(E("p"), b));
      });
      supporting.append(box);
    }
    if (isExcluded(r)) c.append(A(E("aside", null, "exclusion-banner"), E("strong", `${r.exclusion.company || r.organisation || r.name} is excluded`),
      E("p", r.exclusion.reason || "Excluded from new work and application suggestions. History and relationships are kept.")));
    if (r.type === "job") supporting.append(vacancyEvidence(r));
    const dec = decisionFor(r);
    const brief = null;
    if (dec) c.append(decisionPanel(dec));
    const others = decisionsIn("open", "answered").filter(d => d.record_id === r.id && d.id !== dec?.id);
    if (others.length) {
      const more = A(E("details", null, "other-decisions"), E("summary", "Other decisions on this record"));
      others.forEach(d => more.append(decisionButton(d))); supporting.append(more);
    }
    const past = decisionsIn("done", "withdrawn").filter(
      (d) => d.record_id === r.id,
    );
    past.forEach((d) =>
      supporting.append(
        A(
          E("p", null, "decision-outcome"),
          E("b", "Decided: "),
          document.createTextNode(
            `${d.question} ${d.status === "withdrawn" ? "Withdrawn" : choiceLabel(d, d.answer)} → ${d.outcome}`,
          ),
        ),
      ),
    );
    if (r.signals?.length) {
      const sig = A(E("section", null, "detail-section"), E("h3", "In the news"));
      r.signals.forEach((x) => {
        const row = E("p");
        row.append(
          extLink(x.url, x.headline || x.url),
          E(
            "small",
            ` · ${factualDate(x.published)}${x.source ? " · " + x.source : ""}`,
            "muted",
          ),
        );
        sig.append(row);
      });
      supporting.append(sig);
    }
    const reg = r.registry?.[0];
    if (reg) {
      const sec = A(E("section", null, "detail-section"), E("h3", "Companies House"));
      sec.append(
        A(
          E("p"),
          extLink(reg.url, `${reg.company_name} · ${reg.company_number}`),
          E(
            "small",
            ` · ${reg.company_status || "status not stated"}` +
              (reg.incorporated_on ? ` · incorporated ${factualDate(reg.incorporated_on)}` : "") +
              ` · read ${factualDate(reg.observed_at)}`,
            "muted",
          ),
        ),
      );
      if (reg.officers?.length) {
        sec.append(
          E(
            "p",
            "Officers: " +
              reg.officers
                .map((o) => `${o.name} (${o.role}${o.appointed_on ? ", since " + factualDate(o.appointed_on) : ""})`)
                .join("; "),
          ),
        );
      }
      (reg.allotments || []).slice(0, 3).forEach((a) =>
        sec.append(
          A(
            E("p"),
            extLink(a.url, `Shares allotted ${factualDate(a.allotted_on || a.filed_on)}`),
            E("small", ` · ${a.form} filed ${factualDate(a.filed_on)}`, "muted"),
          ),
        ),
      );
      supporting.append(sec);
    }
    if (r.relevance) {
      const why = A(
        E("section", null, "detail-section"),
        A(
          E("h3", null, "why-head"),
          E("span", "Why this ranks here"),
          bandChip(r),
        ),
      );
      const rows = E("div", null, "why");
      r.relevance.reasons.forEach((x) =>
        rows.append(
          A(
            E(
              "div",
              null,
              "why-row " +
                (x.weight > 0 ? "up" : x.weight < 0 ? "down" : "neutral"),
            ),
            E(
              "b",
              x.weight > 0
                ? `+${x.weight}`
                : x.weight < 0
                  ? `−${-x.weight}`
                  : "·",
            ),
            E("span", x.text),
          ),
        ),
      );
      why.append(
        rows,
        E(
          "small",
          "Derived from recorded facts and your targets in config/relevance.yml. It never changes this record.",
          "muted",
        ),
      );
      supporting.append(r.type === "job" ? disclosure("Score breakdown", why, "score-details") : why);
    }
    const actions = E("div", null, "detail-actions");
    [
      ["Edit record", () => openEdit(r)],
      ["Change record state", () => openTransition(r)],
      ["Update work state", () => openFollowup(r)],
      ["Log interaction", () => openInteraction(r)],
      ["Link record", () => openLink(r)],
    ].forEach(([x, fn]) => {
      if (x === "Change record state" && !Object.keys(r.transitions || {}).length)
        return;
      const b = E("button", x);
      b.type = "button";
      b.onclick = fn;
      actions.append(b);
    });
    if (dec || brief || focused || isExcluded(r)) {
      const tools = E("details", null, "edit-details");
      tools.append(E("summary", "Edit details"));
      if (r.type === "person") tools.append(listPanel(r));
      tools.append(actions);
      supporting.append(tools);
    } else supporting.append(actions);
    const facts = [
      ...(focused ? ["summary"] : []),
      "organisation",
      "role",
      "stage",
      "status",
      "relationship",
      "kind",
      "priority",
      "preferred_channel",
      "preferred_ask",
      "next_action",
      "status_reason",
      "waiting_on",
      "owner",
      "last_activity",
      "last_touch",
      "date",
      "published_deadline",
      "source_lifecycle",
      "location",
      "workplace",
      "pay",
      "engagement",
      "ir35",
      "verification",
      "evidence_urls",
      "email",
      "phone",
      "url",
      "detail_url",
      "apply_url",
      "notes",
      "known_context",
    ];
    const dl = E("dl", null, "facts");
    facts.forEach((k) => {
      if (
        r[k] === null ||
        r[k] === undefined ||
        r[k] === "" ||
        (k === "source_lifecycle" && r[k] === "unknown")
      )
        return;
      const dd = E("dd"),
        raw = String(r[k]);
      if (["url", "detail_url", "apply_url", "evidence_urls"].includes(k)) {
        raw.split(/\s*;\s*/).forEach((url, i) => {
          try {
            const u = new URL(url);
            if (!["http:", "https:"].includes(u.protocol)) throw new Error();
            const a = E("a", urlLabel(u) + " ↗");
            a.href = u.href;
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            dd.append(
              a,
              i < raw.split(/\s*;\s*/).length - 1
                ? E("br")
                : document.createTextNode(""),
            );
          } catch {
            dd.append(E("span", url));
          }
        });
      } else if (/^(date|published_deadline|last_activity|last_touch)$/.test(k))
        dd.textContent = factualDate(raw);
      else if (k === "engagement") dd.textContent = engagementLabel(r);
      else linkify(dd, fieldValue(k, raw));
      dl.append(E("dt", fieldName(k)), dd);
    });
    if (dl.childNodes.length) {
      const factsSection = A(E("section", null, "detail-section"), E("h3", "Details"), dl);
      supporting.append(r.type === "job" ? disclosure("Full advert, notes and facts", factsSection, "fact-details") : factsSection);
    }
    const byRecord = new Map();
    related(r.id).forEach(([x, l]) => {
      if (!byRecord.has(x.id)) byRecord.set(x.id, [x, new Set()]);
      byRecord.get(x.id)[1].add(relationName(l, r.id));
    });
    const links = [...byRecord.values()];
    const ls = E("div");
    links.forEach(([x, names]) => {
      if (names.size > 1) names.delete("Related");
      const b = E("button", null, "link-item");
      b.type = "button";
      b.dataset.id = x.id;
      b.onclick = () => openRecord(x.id);
      b.append(
        E("strong", x.name),
        E("small", [typeName(x.type), ...names].join(" · ")),
      );
      ls.append(b);
    });
    supporting.append(
      A(
        E("section", null, "detail-section"),
        E("h3", "Linked records"),
        links.length ? ls : E("p", "No linked records.", "muted"),
      ),
    );
    const timeline = A(
      E("section", null, "detail-section"),
      E("h3", "History"),
      E("p", "Loading history…", "muted"),
    );
    supporting.append(r.type === "job" ? disclosure("History", timeline, "history-details") : timeline);
    timelines.render(r, timeline);
    const evidence = E("section", null, "detail-section");
    evidence.append(E("h3", "Evidence and open questions"));
    for (const review of r.research || []) {
      const block = E("div", null, "research-note");
      block.append(
        E(
          "small",
          `Reviewed ${factualDate(review.reviewed_at || review.observed_at)}${review.verification ? " · " + typeName(review.verification) : ""}`,
        ),
      );
      for (const k of [
        "desk_summary",
        "summary",
        "signal",
        "mandate_evidence",
        "public_contact_route",
      ])
        if (review[k]) block.append(L("p", review[k]));
      if (review.source_date)
        block.append(E("p", `Evidence date: ${factualDate(review.source_date)}`));
      for (const [key, label] of [["facts", "Facts"], ["assumptions", "Assumptions"]])
        if (Array.isArray(review[key]) && review[key].length)
          block.append(L("p", label + ": " + review[key].join("; ")));
      if (review.suggested_task)
        block.append(L("p", "Suggested task: " + review.suggested_task));
      if (review.decisive_unknowns?.length)
        block.append(
          L("p", "Still unknown: " + review.decisive_unknowns.join("; ")),
        );
      for (const url of review.source_urls || []) {
        if (!/^https?:\/\//i.test(url)) continue;
        const a = E("a", url);
        a.href = url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        block.append(a, E("br"));
      }
      evidence.append(block);
    }
    if (['person', 'agency'].includes(r.type) && r.research?.length) supporting.append(evidence);
    if (focused) c.append(disclosure("Sources and details", supporting, "brief-supporting"));
  }

  function listPanel(r) {
    const state = getState();
    const box = A(E("form", null, "detail-section list-panel"), E("h3", "Your list"));
    const vocab = state.vocabularies?.person || {};
    const pick = (text, name, value, options) => {
      const field = A(E("label", null, "field"), E("span", text)), select = E("select");
      select.name = name;
      options.forEach(([v, label]) => { const o = E("option", label); o.value = v; select.append(o); });
      select.value = value || "";
      select.onchange = () => { navigation.setDirty(true); };
      field.append(select);
      return field;
    };
    const textInput = (text, name, value, type = "text", placeholder = "") => {
      const field = A(E("label", null, "field"), E("span", text)), input = E("input");
      input.name = name; input.type = type; input.value = value || "";
      if (placeholder) input.placeholder = placeholder;
      input.oninput = () => { navigation.setDirty(true); };
      field.append(input);
      return field;
    };
      const textArea = (text, name, value, placeholder = "") => {
      const field = A(E("label", null, "field span-2"), E("span", text)), area = E("textarea");
      area.name = name; area.rows = 2; area.maxLength = 1000; area.value = value || "";
      if (placeholder) area.placeholder = placeholder;
      area.oninput = () => { navigation.setDirty(true); };
      field.append(area);
      return field;
    };
    const ownNoteOf = (ctx) => {
      for (const line of String(ctx || "").split(/\r?\n/)) {
        if (line.startsWith("Your note") && line.includes(":")) return line.slice(line.indexOf(":") + 1).trim();
      }
      return "";
    };
    const updateOwnNote = (ctx, nextNote) => {
      const lines = String(ctx || "").split(/\r?\n/).filter((l) => !l.startsWith("Your note"));
      if (nextNote) lines.push(`Your note: ${nextNote}`);
      return lines.filter(Boolean).join("\n");
    };
    const named = (names, values) => values.map((v) => [v, names[v] || typeName(v)]);
    const moves = (field, names) => {
      const allowed = r.transitions?.[field] || [];
      return [[r[field] || "", r[field] ? names[r[field]] || typeName(r[field]) : "Not stated"], ...named(names, allowed.filter((v) => v !== r[field]))];
    };
    const initialNote = ownNoteOf(r.known_context);
    const grid = E("div", null, "form-grid");
    const priorityField = pick("Place on your list", "priority", r.priority, [["", "Not on your list"], ...named(PRIORITY_NAMES, vocab.priority || [])]);
    const relationshipField = pick("How well you know them", "relationship", r.relationship, moves("relationship", RELATIONSHIP_NAMES));
    relationshipField.querySelector("select").addEventListener("change", (e) => {
      if (e.target.value === "do_not_contact") priorityField.querySelector("select").value = "";
    });
    grid.append(
      priorityField,
      relationshipField,
      pick("Who they are", "kind", r.kind, moves("kind", KIND_NAMES)),
      pick("Channel", "preferred_channel", r.preferred_channel, [["", "Not set"], ...named(CHANNEL_NAMES, vocab.preferred_channel || [])]),
      pick("Kind of ask", "preferred_ask", r.preferred_ask, [["", "Not set"], ...named(ASK_NAMES, vocab.preferred_ask || [])]),
      textInput("Employer / organisation", "organisation", r.organisation, "text", r.linkedin?.observed_company || "Company or agency"),
      textInput("Role / title", "role", r.role, "text", r.linkedin?.observed_role || "Role or title"),
      textInput("Phone / WhatsApp", "phone", r.phone, "tel", "07… or +44…"),
      textInput("Email address", "email", r.email, "email", "name@company.com"),
      textArea("Relationship note", "own_note", initialNote, "How you know them and relevant context"),
    );
    const save = E("button", "Save", "primary"), status = E("span", "", "muted");
    save.type = "submit";
    status.setAttribute("role", "status");
    box.append(grid, A(E("div", null, "form-actions"), save, status));
    box.onsubmit = async (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(box));
      const fields = Object.fromEntries(["priority", "preferred_channel", "preferred_ask", "organisation", "role", "phone", "email"]
        .filter((k) => {
          const next = (data[k] || "").trim();
          const prev = (r[k] || "").trim();
          return next !== prev;
        }).map((k) => [k, (data[k] || "").trim() || (["organisation", "role", "phone", "email"].includes(k) ? "" : null)]));
      if (data.relationship === "do_not_contact") delete fields.priority;
      const nextNote = (data.own_note || "").trim();
      if (nextNote !== initialNote) {
        fields.known_context = updateOwnNote(r.known_context, nextNote);
      }
      const moved = ["relationship", "kind"].filter((k) => data[k] && data[k] !== r[k]);
      if (!Object.keys(fields).length && !moved.length) { status.textContent = "Nothing changed."; return; }
      save.disabled = true; status.textContent = "Saving…";
      try {
        let revision = r.revision;
        const url = `/api/records/${encodeURIComponent(r.id)}`, json = { "Content-Type": "application/json" };
        for (const field of moved)
          revision = (await api(`${url}/transition`, { method: "POST", headers: json,
            body: JSON.stringify({ field, to: data[field], revision, note: "Changed on your list" }) })).record.revision;
        if (Object.keys(fields).length)
          revision = (await api(url, { method: "PATCH", headers: json, body: JSON.stringify({ ...fields, revision }) })).record.revision;
        navigation.setDirty(false);
        await load("save");
        if (record(r.id)) renderDetail(record(r.id));
        showNotice(`Saved: ${r.name}'s place on your list.`);
      } catch (error) {
        save.disabled = false;
        status.textContent = error.message;
      }
    };
    return box;
  }

  function showRecord(r) {
    kind.textContent = typeName(r.type);
    renderDetail(r);
    content.scrollTop = 0;
    if (!dialog.open) dialog.showModal();
  }
  function showDecision(d, r) {
    if (r) {
      kind.textContent = `Decision #${d.id} · ${d.record_name || r.name}`;
      renderDetail(r);
      content.scrollTop = 0;
      if (!dialog.open) dialog.showModal();
      return;
    }
    kind.textContent = `Workspace decision · #${d.id}`;
    navigation.setDirty(false);
    content.replaceChildren(A(E("div", null, "detail-title"), E("h2", d.question)), decisionPanel(d));
    if (!dialog.open) dialog.showModal();
    content.scrollTop = 0;
  }
  return { render: renderDetail, showRecord, showDecision, openContactDetails };
}
