// LinkedIn import review on People: each imported candidate with its evidence, and accept, merge or reject.
import { E, A, L, val, typeName, extLink } from "./dom.js";

// Also used by the LinkedIn import dialog in app.js.
export function readableValue(value) {
  if (Array.isArray(value))
    return value.map(readableValue).filter(Boolean).join("; ");
  if (value && typeof value === "object") {
    const name =
        value.name ||
        value.full_name ||
        value.label ||
        value.record_name ||
        value.id ||
        "Match",
      strength = value.strength || value.match_strength || value.score;
    return strength !== undefined ? `${name} (${typeName(strength)})` : name;
  }
  return String(value);
}

export function createImportReview({ getState, factualDate, openModal, mutate, showAlert }) {
  function evidenceLines(p) {
    const e = p.evidence || {},
      t = e.threads || {},
      out = [];
    if (t.count)
      out.push(
        `${t.count} LinkedIn thread${t.count > 1 ? "s" : ""}${t.two_way ? `, ${t.two_way} two-way` : ", none two-way"}${t.last_sent ? `; you last wrote ${factualDate(t.last_sent)}` : t.last ? `; last message ${factualDate(t.last)}` : ""}`,
      );
    if (e.endorsed_you) out.push(`Endorsed you ×${e.endorsed_you}`);
    if (e.you_endorsed) out.push(`You endorsed them ×${e.you_endorsed}`);
    (e.recommendations || []).forEach((d) =>
      out.push(d === "received" ? "Wrote you a recommendation" : "You wrote them a recommendation"),
    );
    if (e.invitation)
      out.push(
        `Invitation ${e.invitation.direction === "outgoing" ? "you sent" : "they sent"} ${factualDate(e.invitation.date)}`,
      );
    return out;
  }

  function card(item) {
    const card = E("article", null, "review-card"),
      payload = item.payload || {},
      name = payload.name || payload.full_name || "LinkedIn candidate";
    if (item.kind !== "linkedin_person" || !payload.evidence) {
      card.append(E("strong", name), E("p", typeName(item.source || item.kind || "Import"), "muted"));
      const dl = E("dl", null, "facts");
      Object.entries(payload)
        .filter(([, v]) => v !== null && v !== undefined && v !== "")
        .forEach(([k, v]) => dl.append(E("dt", typeName(k)), L("dd", readableValue(v))));
      card.append(dl);
    } else {
      card.append(
        A(E("div", null, "review-head"), E("strong", name),
          payload.warning ? E("span", "Former employer", "chip state-blocked") : null),
        E("p", [payload.observed_role, payload.observed_company].filter(Boolean).join(" at ") +
          (payload.connected_date ? ` · connected ${factualDate(payload.connected_date)}` : ""), "muted"),
      );
      const ev = E("ul", null, "evidence");
      evidenceLines(payload).forEach((x) => ev.append(L("li", x)));
      if (!ev.childNodes.length) ev.append(E("li", "No messages or endorsements; connected only."));
      card.append(ev);
      if (payload.org_match)
        card.append(E("p", `On accept: linked to ${payload.org_match.name} (${payload.org_match.relation === "consultant_at" ? "consultant at" : "works at"}, unverified).`, "review-note"));
      if (payload.warning) card.append(E("p", payload.warning, "review-note warn"));
      if (payload.url) card.append(extLink(payload.url, "LinkedIn profile ↗"));
    }
    (item.matches || []).forEach((m) =>
      card.append(E("p", `Already in your people: ${m.name} (${m.match === "identifier" ? "same profile or email" : "same name"}). Merge adds this evidence to them${m.match === "identifier" ? "" : ", or accept as new if this is someone else"}.`, "review-note warn")),
    );
    const actions = E("div", null, "review-actions");
    (item.actions || []).forEach((a) => {
      const b = E("button", a.label, a.primary ? "primary" : "");
      b.type = "button";
      b.onclick = () => resolve(item, a, actions);
      actions.append(b);
    });
    card.append(actions);
    return card;
  }

  async function resolve(item, action, actions) {
    const send = async (extra) => {
      const buttons = [...actions.querySelectorAll("button")];
      buttons.forEach((b) => (b.disabled = true));
      try {
        await mutate(`/api/review/${encodeURIComponent(item.id)}`, {
          resolution: action.resolution,
          ...(action.target_id ? { target_id: action.target_id } : {}),
          ...(action.accept_as_new ? { accept_as_new: true } : {}),
          ...extra,
          revision: item.revision,
        });
      } finally {
        buttons.forEach((b) => (b.disabled = false));
      }
    };
    if (action.pick_target) {
      const g = E("div", null, "form-grid"),
        l = E("label", null, "span-2"),
        s = E("select");
      l.append(E("span", "Merge into person"));
      s.name = "target_id";
      getState().records
        .filter((r) => r.type === "person")
        .sort((a, b) => val(a.name).localeCompare(val(b.name), "en-GB"))
        .forEach((r) => {
          const o = E("option", `${r.name}${r.organisation ? " · " + r.organisation : ""}`);
          o.value = r.id;
          s.append(o);
        });
      l.append(s);
      g.append(l);
      return openModal("Merge imported person", g, (d) => send({ target_id: d.target_id }), "Merge");
    }
    try {
      await send({});
    } catch (e) {
      showAlert(e.message);
    }
  }

  // Shown below the groups on People, only while candidates wait.
  function render(container) {
    const queue = getState().review_queue || [];
    if (!queue.length) return;
    const head = A(E("div", null, "section-head"), E("h2", "Imported people to review"), E("span", queue.length, "count"));
    head.id = "import-review";
    container.append(head, E("p", "Accept, merge or reject each LinkedIn candidate before it joins your people.", "muted section-note"));
    queue.forEach((item) => container.append(card(item)));
  }

  return { render };
}
