// The record dialogs: add a person, create, edit, change state, update work state, log an
// interaction, link records and import LinkedIn connections. Each opens the shared modal and saves
// through the page's mutate/api helpers; none sends anything.
import { E, A, val, typeName } from "./dom.js";
import { api } from "./api.js";
import { compact } from "./forms.js";
import { readableValue } from "./import-review.js";
import { RELATIONSHIP_NAMES, KIND_NAMES, PRIORITY_NAMES, CHANNEL_NAMES, ASK_NAMES } from "./labels.js";

const WORK_STATES = ["needs_review", "ready", "in_progress", "waiting", "blocked", "paused", "done"];

export function createRecordForms({ getState, getRecord, openModal, label, selectField, recordFields, mutate, load,
  openRecord, showNotice, showAlert, setDirty, recordState, canonicalAction }) {
  // Someone new, often from an introduction: who they are, who introduced them and their place on the list.
  function openAddPerson() {
    const state = getState();
    const vocab = state.vocabularies?.person || {};
    const g = E("div", null, "form-grid");
    const choose = (text, name, options, value = "") => {
      const l = A(E("label"), E("span", text)), sel = E("select");
      sel.name = name;
      options.forEach(([v, label]) => { const o = E("option", label); o.value = v; sel.append(o); });
      sel.value = value;
      sel.onchange = () => { setDirty(true); };
      l.append(sel);
      return l;
    };
    const named = (names, values) => values.map((v) => [v, names[v] || typeName(v)]);
    const introducers = state.records.filter((r) => r.type === "person" && r.relationship !== "do_not_contact")
      .sort((a, b) => val(a.name).localeCompare(val(b.name), "en-GB")).map((r) => [r.id, r.name]);
    g.append(
      label("Name", "name", "", "text", false, true),
      label("Role", "role"),
      label("Organisation", "organisation"),
      choose("Introduced by", "introducer", [["", "Nobody recorded"], ...introducers]),
      choose("How well you know them", "relationship", [["", "Not stated"], ...named(RELATIONSHIP_NAMES, vocab.relationship || [])]),
      choose("Who they are", "kind", [["", "Not stated"], ...named(KIND_NAMES, vocab.kind || [])]),
      choose("Place on your list", "priority", [["", "Not on your list"], ...named(PRIORITY_NAMES, vocab.priority || [])], "first"),
      choose("Channel", "preferred_channel", [["", "Not set"], ...named(CHANNEL_NAMES, vocab.preferred_channel || [])]),
      choose("Kind of ask", "preferred_ask", [["", "Not set"], ...named(ASK_NAMES, vocab.preferred_ask || [])]),
      label("How you know them (optional)", "known_context", "", "textarea", true),
    );
    openModal("Add a person", g, async (data) => {
      const { introducer, ...fields } = data;
      // His words about someone are a note like those from his spreadsheet, so talking points use them.
      const today = new Date().toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
      if (fields.known_context?.trim()) fields.known_context = `Your note (${today}): ${fields.known_context.trim()}`;
      const created = await api("/api/records", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(compact({ type: "person", ...fields })) });
      if (introducer) {
        const by = getRecord(introducer);
        await api("/api/links", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ from_id: introducer, to_id: created.record.id, relation: "introducer", revision: by.revision }) });
      }
      await load("save");
      openRecord(created.record.id);
      showNotice(`Added ${created.record.name}${introducer ? `, introduced by ${getRecord(introducer)?.name}` : ""}.`);
    }, "Add");
  }

  function openCreate(prefill = {}) {
    openModal("New record", recordFields(prefill, true), async (data) => mutate("/api/records", compact(data)), "Create");
  }

  function openEdit(r) {
    openModal("Edit record", recordFields(r), async (data) =>
      mutate(`/api/records/${encodeURIComponent(r.id)}`, { ...data, revision: r.revision }, "PATCH"));
  }

  function openTransition(r) {
    const moves = r.transitions || {},
      fields = Object.keys(moves);
    if (!fields.length) return showAlert("No further record state changes are allowed from here.");
    const g = E("div", null, "form-grid"),
      field = selectField("Field", "field", fields[0], fields),
      target = selectField("New value", "to", "", moves[fields[0]]);
    const fs = field.querySelector("select"),
      ts = target.querySelector("select");
    fs.onchange = () => {
      ts.replaceChildren();
      (moves[fs.value] || []).forEach((x) => {
        const o = E("option", typeName(x));
        o.value = x;
        ts.append(o);
      });
      setDirty(true);
    };
    g.append(field, target, label("Note", "note", "", "textarea", true));
    openModal("Change record state", g, (d) =>
      mutate(`/api/records/${encodeURIComponent(r.id)}/transition`, { ...d, revision: r.revision }), "Change");
  }

  function openFollowup(r) {
    const g = E("div", null, "form-grid");
    g.append(
      selectField("Working state", "status", recordState(r), WORK_STATES),
      label("Next action", "action", canonicalAction(r), "textarea", true, true),
      selectField("Owner", "owner", r.owner || r.followup?.owner || "owner", ["owner", "agent"]),
      label("Reason", "reason", r.status_reason || r.followup?.reason || ""),
      label("Waiting on", "waiting_on", r.waiting_on || r.followup?.waiting_on || ""),
      label("Notes", "notes", r.followup?.notes || "", "textarea", true),
    );
    openModal("Update work state", g, async (d) => mutate("/api/followup", { id: r.id, revision: r.revision, ...d }));
  }

  function openInteraction(r) {
    const g = E("div", null, "form-grid");
    g.append(
      label("When", "at", new Date().toISOString().slice(0, 10), "date"),
      selectField("Channel", "channel", "other", getState().vocabularies?.interaction?.channel || ["other"]),
      selectField("Direction", "direction", "note", ["in", "out", "note"]),
      label("Summary", "summary", "", "textarea", true),
      label("Evidence reference", "evidence_ref", "", "text", true),
    );
    openModal("Log interaction", g, async (d) =>
      mutate("/api/interactions", { record_id: r.id, revision: r.revision, ...d }), "Log");
  }

  function openLink(r) {
    const state = getState();
    const g = E("div", null, "form-grid");
    const l = E("label", null, "span-2");
    l.append(E("span", "Record"));
    const s = E("select");
    s.name = "to_id";
    state.records
      .filter((x) => x.id !== r.id)
      .sort((a, b) => val(a.name).localeCompare(val(b.name)))
      .forEach((x) => {
        const o = E("option", `${x.name} · ${typeName(x.type)}`);
        o.value = x.id;
        s.append(o);
      });
    l.append(s);
    g.append(
      l,
      selectField("Relationship", "relation", (state.relations || [])[0] || "related", state.relations || ["related"]),
      label("Notes", "notes", "", "textarea", true),
    );
    openModal("Link record", g, async (d) => mutate("/api/links", { from_id: r.id, revision: r.revision, ...d }), "Link");
  }

  function openImport() {
    const g = E("div", null, "form-grid");
    const l = E("label", null, "span-2");
    l.append(E("span", "LinkedIn CSV file"));
    const i = E("input");
    i.type = "file";
    i.accept = ".csv,text/csv";
    i.required = true;
    l.append(i);
    g.append(l, E("p", "You will review candidates before they are added.", "muted"));
    openModal("Import LinkedIn CSV", g, async () => {
      const f = i.files[0];
      if (!f) throw new Error("Choose a CSV file.");
      const result = await api("/api/import/linkedin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csv: await f.text() }),
      });
      const invalid = Array.isArray(result.invalid) ? result.invalid : [],
        invalidCount = invalid.length || Number(result.invalid || 0),
        details = invalid.slice(0, 5).map((x) => readableValue(x)).join(" · ");
      await load("save");
      showAlert(`${result.queued ?? 0} queued, ${result.existing ?? 0} already present, ${invalidCount} invalid.${details ? " " + details : ""}`);
    }, "Preview import");
  }

  return { openAddPerson, openCreate, openEdit, openTransition, openFollowup, openInteraction, openLink, openImport };
}
