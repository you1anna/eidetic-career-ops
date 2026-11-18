import { E, typeName } from "./dom.js";
export function createFormHelpers(onChange) {
  function label(
    text,
    name,
    value = "",
    type = "text",
    span = false,
    required = false,
  ) {
    const l = E("label", null, span ? "span-2" : "");
    l.append(E("span", text));
    let input;
    if (type === "textarea") {
      input = E("textarea");
      input.rows = 4;
    } else {
      input = E("input");
      input.type = type;
    }
    input.name = name;
    input.required = required;
    input.value = value ?? "";
    input.oninput = onChange;
    l.append(input);
    return l;
  }
  function selectField(text, name, value, options) {
    const l = E("label");
    l.append(E("span", text));
    const s = E("select");
    s.name = name;
    options.forEach((x) => {
      const o = E("option", typeName(x));
      o.value = x;
      o.selected = x === value;
      s.append(o);
    });
    s.onchange = onChange;
    l.append(s);
    return l;
  }
  function recordFields(r = {}, creating = false) {
    const g = E("div", null, "form-grid");
    if (creating)
      g.append(
        selectField("Type", "type", r.type || "person", [
          "person",
          "company",
          "agency",
          "job",
          "opportunity",
          "tender",
          "event",
          "source",
        ]),
      );
    g.append(
      label("Name", "name", r.name || ""),
      label("Organisation", "organisation", r.organisation || ""),
      label("Role", "role", r.role || ""),
      label("Summary", "summary", r.summary || "", "textarea", true),
      label("Location", "location", r.location || ""),
      label("URL", "url", r.url || "", "url"),
      label("Email", "email", r.email || "", "email"),
      label("Phone", "phone", r.phone || ""),
    );
    const more = E("details", null, "more-fields span-2"),
      summary = E("summary", "More facts"),
      inner = E("div", null, "form-grid");
    inner.append(
      label("Pay", "pay", r.pay || ""),
      label("Engagement", "engagement", r.engagement || ""),
      label("Workplace", "workplace", r.workplace || ""),
      label("IR35", "ir35", r.ir35 || ""),
      label("Factual date", "date", r.date || ""),
      label(
        "Published deadline",
        "published_deadline",
        r.published_deadline || "",
      ),
      label(
        "Evidence URLs",
        "evidence_urls",
        r.evidence_urls || "",
        "textarea",
        true,
      ),
      label(
        "Verification",
        "verification",
        r.verification || "",
        "textarea",
        true,
      ),
      label(
        "Known context",
        "known_context",
        r.known_context || "",
        "textarea",
        true,
      ),
      label("Notes", "notes", r.notes || "", "textarea", true),
    );
    more.append(summary, inner);
    g.append(more);
    return g;
  }
  return { label, selectField, recordFields };
}
export function compact(data) {
  return Object.fromEntries(Object.entries(data).filter(([, v]) => v !== ""));
}
