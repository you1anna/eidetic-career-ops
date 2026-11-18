// Timeline responses update only their own section, never the surrounding form.
import { E, A, L, typeName } from "./dom.js";
import { api } from "./api.js";
import { fieldName, fieldValue, factualDate, choiceLabel } from "./record-presentation.js";
import { ACTOR_NAMES } from "./labels.js";

const MAX_CACHED_TIMELINES = 8;

export function createRecordTimeline({ getRecord: record }) {
  const timelineRequests = new Map();
  const timelineResponses = new Map();
  let generation = 0;
  function readTimeline(r, key) {
    if (timelineResponses.has(key)) {
      const data = timelineResponses.get(key);
      timelineResponses.delete(key);
      timelineResponses.set(key, data);
      return data;
    }
    if (timelineRequests.has(key)) return timelineRequests.get(key);
    const request = api(`/api/records/${encodeURIComponent(r.id)}/timeline`).then(data => {
      // clear() may have detached this request and started another for the same record.
      if (timelineRequests.get(key) === request) {
        timelineRequests.delete(key);
        timelineResponses.set(key, data);
        while (timelineResponses.size > MAX_CACHED_TIMELINES)
          timelineResponses.delete(timelineResponses.keys().next().value);
      }
      return data;
    }, error => {
      if (timelineRequests.get(key) === request) timelineRequests.delete(key);
      throw error;
    });
    timelineRequests.set(key, request);
    return request;
  }
  async function renderTimeline(r, section) {
    const key = `${r.id}/${r.revision}`;
    const started = generation;
    let data;
    try {
      data = await readTimeline(r, key);
      if (started !== generation || !section.isConnected || record(r.id)?.revision !== r.revision) return;
      if (data.revision !== r.revision) {
        section.replaceChildren(
          E("h3", "History"),
          E(
            "p",
            "This record changed elsewhere. Refresh to read its latest history.",
            "muted",
          ),
        );
        return;
      }
      const events = [
        ...data.interactions.map((x) => ({ ...x, _kind: "Interaction" })),
        ...data.history
          .filter((x) => x.field !== "interaction")
          .map((x) => ({ ...x, _kind: "Change" })),
      ].sort((a, b) =>
        String(b.at || b.created_at || "").localeCompare(
          String(a.at || a.created_at || ""),
        ),
      );
      const list = E("div", null, "timeline");
      events.forEach((x) =>
        list.append(
          A(
            E("div", null, "timeline-item"),
            L(
              "strong",
              x.summary ||
                x.note ||
                (x.field
                  ? `${fieldName(x.field)}: ${x.from_value ? fieldValue(x.field, x.from_value) : "not recorded"} → ${x.to_value ? fieldValue(x.field, x.to_value) : "cleared"}`
                  : x._kind),
            ),
            E(
              "small",
              `${factualDate(x.at || x.created_at)} · ${x._kind === "Interaction" ? typeName(x.channel || "interaction") : ACTOR_NAMES[x.actor] || typeName(x.actor || "change")}`,
            ),
          ),
        ),
      );
      section.replaceChildren(
        E("h3", "History"),
        events.length ? list : E("p", "No history recorded.", "muted"),
      );
      if (data.drafts?.length) {
        const saved=A(E('details',null,'saved-wording-history'),E('summary',`Saved wording (${data.drafts.length} versions)`));
        for(const draft of data.drafts) saved.append(A(E('article',null,'historical-draft'),E('h4',draft.title || 'Saved draft'),
          E('small',`${factualDate(draft.created_at || draft.at)} · version ${draft.version_id || draft.id}`),E('pre',draft.text,'draft')));
        section.append(saved);
      }
      if (data.outcomes?.length) {
        const saved=A(E('details',null,'outcome-history'),E('summary','Contact and reply history'));
        for(const outcome of data.outcomes) {
          const row=A(E('article'),E('strong',typeName(outcome.kind)+(outcome.corrected_at ? ' · corrected' : '')),
            E('small',factualDate(outcome.at)),L('p',outcome.note || ''));
          if(outcome.snapshot?.message)row.append(E('pre',outcome.snapshot.message,'draft'));
          saved.append(row);
        }section.append(saved);
      }
      if (data.decisions?.length) {
        const archive = A(E("details", null, "decision-history"), E("summary", "Full decision history"));
        data.decisions.forEach(d => {
          const item = A(E("details"), E("summary", `#${d.id} ${d.question} · ${typeName(d.status)}`),
            L("p", d.context || ""), L("p", d.reason || ""),
            E("p", `Answer: ${choiceLabel(d, d.answer) || "Not answered"}`), L("p", d.outcome || ""));
          if (d.draft) item.append(E("pre", d.draft.text, "draft"));
          (d.thread || []).forEach(t => item.append(L("p", `${t.by === "owner" ? "You" : "Agent"}: ${t.text || t.label || ""}`)));
          archive.append(item);
        }); section.append(archive);
      }
    } catch (error) {
      if (started !== generation || !section.isConnected || record(r.id)?.revision !== r.revision) return;
      if (timelineResponses.get(key) === data) timelineResponses.delete(key);
      const retry = E("button", "Retry");
      retry.type = "button";
      retry.onclick = () => renderTimeline(r, section);
      section.replaceChildren(
        E("h3", "History"),
        E("p", error.message, "muted"),
        retry,
      );
    }
  }

  return { render: renderTimeline, clear: () => {
    generation++;
    timelineRequests.clear(); timelineResponses.clear();
  } };
}
