// Decision cards and answer forms use the same saved choices and mutation boundary.
import { E, A, L, extLink } from "./dom.js";
import { choiceLabel, hostOf, stamp, firstSentence, laneChip, chip } from "./record-presentation.js";

// Count exactly the decisions whose current declared choices can be answered.
export const decisionsForYou = (items = [], excluded = d => !!d.exclusion) => items.filter(d =>
  d.status === 'open' && d.kind !== 'draft' && !d.read_only && d.choices?.length && !excluded(d));

export function createDecisionViews({ getState, getRecord: record, navigation, mutate, showNotice, isDetailOpen }) {
  const decisionsIn = (...st) => (getState().decisions || []).filter(d => st.includes(d.status));
  function decisionFor(r) {
    return (getState().decisions || []).find((d) => d.record_id === r.id && d.id === navigation.decisionId)
      || decisionsIn("open", "answered").find((d) => d.record_id === r.id);
  }

  function decisionButton(d) {
    const b = E("button", null, "record-card decision-card");
    b.type = "button";
    b.onclick = () => navigation.openDecision(d);
    const r = d.record_id && record(d.record_id);
    b.append(
      A(
        E("span"),
        E("strong", d.question),
        E(
          "small",
          [d.record_name || d.area_label || "Workspace", d.kind_label, d.kind === "failure" && d.created_at ? `Raised ${stamp(d.created_at)}` : ""].filter(Boolean).join(
            " · ",
          ),
        ),
        d.status === "answered"
          ? E(
              "small",
              `Your answer: ${choiceLabel(d, d.answer)}${d.note ? " — " + firstSentence(d.note) : ""}`,
              "recommend",
            )
          : d.recommendation
            ? E(
                "small",
                `Recommendation: ${choiceLabel(d, d.recommendation)}`,
                "recommend",
              )
            : null,
        d.status !== "answered" && (d.context || d.reason)
          ? E("small", d.kind === 'failure' ? d.choices?.find(c => c.id === d.recommendation)?.next || d.reason || firstSentence(d.context) : firstSentence(d.context), "record-context")
          : null,
      ),
      A(
        E("span", null, "card-side"),
        r ? laneChip(r) : null,
        d.status === "answered"
          ? chip("With assistant", "waiting")
          : chip("Your call", "ready"),
      ),
    );
    return b;
  }

  function decisionPanel(d) {
    const p = E("section", null, "decision-panel");
    p.append(E("h3", d.kind_label), E("p", d.question, "decision-question"));
    if (d.context) p.append(L("p", d.context));
    if (d.recommendation)
      p.append(
        L(
          "p",
          `Recommendation: ${choiceLabel(d, d.recommendation)}${d.reason ? " — " + d.reason : ""}`,
          "recommend",
        ),
      );
    if (d.evidence_urls) {
      const ev = E("p", null, "muted");
      d.evidence_urls
        .split(/\s*;\s*/)
        .filter(Boolean)
        .forEach((u, i) => {
          if (i) ev.append(" · ");
          ev.append(extLink(u, hostOf(u) + " ↗"));
        });
      p.append(ev);
    }
    if (d.kind === "draft")
      (d.draft ? [d.draft] : [])
        .forEach((x) =>
          p.append(
            A(
              E("div", null, "decision-draft"),
              E("strong", x.title || "Draft"),
              L("div", x.text, "draft"),
            ),
          ),
        );
    if (d.kind === "draft") { p.append(E("p", "Historical wording and decision, kept read-only.", "muted")); return p; }
    const earlier = d.status === "answered" ? d.thread.slice(0, -1) : d.thread;
    if (earlier.length) {
      const t = E("div", null, "decision-thread");
      earlier.forEach((x) =>
        t.append(
          A(
            E("p"),
            E("b", x.by === "owner" ? `You: ${x.label || ""}` : "Assistant"),
            L("span", x.text ? " — " + x.text : ""),
            E("small", " · " + stamp(x.at), "muted"),
          ),
        ),
      );
      p.append(t);
    }
    if (["done", "withdrawn"].includes(d.status) && !d.can_reconfirm) {
      p.append(L("p", d.outcome || "This decision is closed.")); return p;
    }
    const form = E("form", null, "decision-form");
    if (d.status === "answered") {
      const box = A(
        E("div", null, "decision-answered"),
        E("strong", `Your answer: ${choiceLabel(d, d.answer)}`),
        d.note ? L("p", d.note) : null,
        E(
          "small",
          `${({ queued: "Queued for the next agent session", working: "Agent is working on this", blocked: "Work is blocked; see the recorded reason", completed: "Completed" })[d.execution_status] || "Queued for the next agent session"}. ${d.choices.find((c) => c.id === d.answer)?.next || ""}`,
          "muted",
        ),
      );
      const change = E("button", "Change answer");
      change.type = "button";
      change.onclick = () => {
        box.hidden = true;
        form.hidden = false;
      };
      if (!["working", "blocked"].includes(d.execution_status)) box.append(change);
      p.append(box);
      form.hidden = true;
    }
    const choices = E("div", null, "decision-choices"),
      note = E("textarea"),
      noteText = E("span", "Note (optional)"),
      send = E("button", "Choose an answer", "primary"),
      status = E("span", null, "muted");
    note.name = "note";
    note.rows = 3;
    note.placeholder = "Context for the assistant";
    note.oninput = () => {
      navigation.setDirty(true);
    };
    send.type = "submit";
    send.disabled = true;
    status.setAttribute("role", "status");
    let picked = null;
    d.choices.forEach((ch) => {
      const b = E(
        "button",
        null,
        "decision-choice" + (ch.id === d.recommendation ? " recommended" : ""),
      );
      b.type = "button";
      b.setAttribute("aria-pressed", "false");
      b.append(
        E(
          "strong",
          ch.label + (ch.id === d.recommendation ? " · recommended" : ""),
        ),
        E("small", ch.next),
      );
      b.onclick = () => {
        picked = ch;
        choices
          .querySelectorAll("button")
          .forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        noteText.textContent = ch.note
          ? `${ch.note} (required)`
          : "Note (optional)";
        note.placeholder = ch.note || "Context for the assistant";
        note.required = !!ch.note;
        send.disabled = false;
        send.textContent = `Send: ${ch.label}`;
        send.scrollIntoView({ block: "nearest", behavior: "smooth" });
      };
      choices.append(b);
    });
    form.onsubmit = async (e) => {
      e.preventDefault();
      if (!picked) return;
      send.disabled = true;
      status.textContent = "Sending…";
      try {
        navigation.setDirty(false);
        await mutate(`/api/decisions/${d.id}`, {
          choice: picked.id,
          note: note.value,
          revision: d.revision,
        });
      } catch (err) {
        navigation.setDirty(true);
        status.textContent = err.message;
        send.disabled = false;
        return;
      }
      if (isDetailOpen()) navigation.closeDetail();
      showNotice(`Answer saved: ${picked.label} — “${d.question}” is queued for the next agent session.`);
    };
    form.append(
      choices,
      A(E("label", null, "decision-note"), noteText, note),
      A(E("div", null, "form-actions"), send, status),
    );
    p.append(form);
    return p;
  }

  return { decisionsIn, decisionFor, decisionButton, decisionPanel };
}
