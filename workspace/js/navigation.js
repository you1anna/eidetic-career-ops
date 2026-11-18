// One owner for page/record/decision selection, action routes, the detail back-stack and dirty state.
// Browser and view effects are injected so route transitions can be checked without a DOM or server.
export const PAGES = [["week", "This week"], ["people", "People"], ["work", "Jobs"], ["dashboard", "Dashboard"]];
export const RESEARCH = [];
const WEEK_GROUPS = ['act-now', 'waiting', 'in-progress', 'discovery', 'needs-checking'];
const REDIRECTS = { status: "dashboard", admin: "dashboard", overview: "week", network: "people", jobs: "work", sources: "people", companies: "work", pipeline: "work", outreach: "work", templates: "work", research: "work" };
export const isResearch = key => RESEARCH.some(x => x[0] === key);
const knownTab = key => PAGES.some(x => x[0] === key) || isResearch(key);

export function createNavigation({ getRecord, getDecisions, sheet, render, onTabChange, seen,
  location, history, confirmDiscard }) {
  let tab = "week", researchTab = "outreach", selected = null, selectedDecision = null,
    selectedAction = null, selectedLead = null, selectedRoute = null, dirty = false, weekGroup = null;
  const detailStack = [];
  function canLeave() {
    if (!dirty) return true;
    if (!confirmDiscard()) return false;
    dirty = false;
    return true;
  }
  function setTab(key) {
    tab = key;
    if (tab !== "week") weekGroup = null;
    if (isResearch(tab)) researchTab = tab;
  }
  function clearSelection() {
    selected = selectedDecision = selectedAction = selectedLead = selectedRoute = null;
  }
  function openRecord(id, push = true, actionId = null, back = false) {
    if (!canLeave()) return;
    const r = getRecord(id);
    if (!r) return;
    if (!back && sheet.isOpen() && selected && selected !== id) detailStack.push([selected, selectedAction]);
    selected = id;
    selectedLead = null;
    selectedDecision = null;
    selectedAction = actionId;
    selectedRoute = `#${tab}/${encodeURIComponent(id)}${actionId ? '/action/' + encodeURIComponent(actionId) : ''}`;
    if (push) location.hash = selectedRoute;
    sheet.showRecord(r);
  }
  function openDecision(d, push = true) {
    if (!canLeave()) return;
    if (selected !== d.record_id) selectedAction = null;
    selectedDecision = d.id;
    selectedLead = null;
    selectedRoute = `#${tab}/decision/${d.id}`;
    if (push) location.hash = selectedRoute;
    const r = d.record_id && getRecord(d.record_id);
    selected = r ? d.record_id : null;
    sheet.showDecision(d, r);
  }
  function openJob(id) {
    if (getRecord(id)?.type !== 'job' || !canLeave()) return;
    const previousTab = tab;
    const linked = sheet.isOpen() && selected && selected !== id;
    setTab('work');
    onTabChange();
    openRecord(id);
    if (linked && previousTab !== 'work') detailStack.at(-1).push(previousTab);
  }
  function popBack() {
    const previous = detailStack.pop();
    if (previous?.[2] && previous[2] !== tab) { setTab(previous[2]); onTabChange(); }
    return previous;
  }
  function goBack() {
    if (!detailStack.length || !canLeave()) return;
    const [id, action] = popBack();
    openRecord(id, true, action, true);
  }
  function go(key) {
    if (!canLeave()) return;
    weekGroup = null;
    setTab(REDIRECTS[key] || key);
    location.hash = tab;
    onTabChange();
  }
  function openWeek(group) {
    if (!WEEK_GROUPS.includes(group) || !canLeave()) return;
    setTab('week'); weekGroup = group;
    location.hash = `#week/section/${group}`;
    onTabChange();
  }
  function openLead(id, push = true) {
    if (!canLeave() || !/^[a-f0-9]{64}$/.test(id)) return;
    clearSelection(); selectedLead = id;
    selectedRoute = `#dashboard/lead/${id}`;
    setTab('dashboard');
    if (push) location.hash = selectedRoute;
    sheet.showLead?.(id);
  }
  function closeDetail() {
    if (!canLeave()) return;
    detailStack.length = 0;
    sheet.close();
    clearSelection();
    location.hash = tab === 'week' && weekGroup ? `#week/section/${weekGroup}` : tab;
  }
  function cancelDetail() {
    if (!canLeave()) return false;
    // Native dialog cancellation retains the existing route/back-stack semantics.
    selected = selectedDecision = selectedLead = null;
    return true;
  }
  function routeRecord() {
    if (sheet.isOpen() && dirty && selectedRoute && location.hash !== selectedRoute) {
      if (!canLeave()) {
        history.pushState(null, "", selectedRoute); return;
      }
      dirty = false;
    }
    let [key, id, decisionId, actionId] = location.hash.slice(1).split("/");
    if (REDIRECTS[key]) {
      key = REDIRECTS[key];
      history.replaceState(null, "", "#" + [key, id, decisionId, actionId].filter(x => x !== undefined).join("/"));
    }
    if (id && id !== "decision" && getRecord(decodeURIComponent(id))?.type === "person" && key === "work") {
      key = "people"; history.replaceState(null, "", "#" + [key, id, decisionId, actionId].filter(x => x !== undefined).join("/"));
    }
    if (knownTab(key)) setTab(key);
    if (key === 'week' && id === 'section') {
      weekGroup = WEEK_GROUPS.includes(decisionId) ? decisionId : null;
      id = null;
    } else if (!id) weekGroup = null;
    if (key === 'dashboard' && id === 'lead') {
      render();
      return openLead(decisionId || '', false);
    }
    seen(tab + (id === "decision" ? "/decision" : id ? "/record" : ""));
    if (id === "decision") {
      const d = getDecisions().find(d => String(d.id) === decisionId);
      if (d) return openDecision(d, false);
    }
    if (id && getRecord(decodeURIComponent(id)))
      return openRecord(decodeURIComponent(id), false, decisionId === "action" ? decodeURIComponent(actionId || "") : null);
    if ((selected || selectedDecision || selectedLead) && sheet.isOpen()) {
      if (!canLeave()) {
        history.pushState(null, "", selectedRoute || `#${tab}`); return;
      }
      sheet.close();
      clearSelection();
    }
    render();
  }
  return {
    get tab() { return tab; }, get weekGroup() { return weekGroup; }, get researchTab() { return researchTab; },
    get recordId() { return selected; }, get decisionId() { return selectedDecision; },
    get leadId() { return selectedLead; },
    get actionId() { return selectedAction; }, get route() { return selectedRoute; },
    get dirty() { return dirty; }, get backTarget() { return detailStack.at(-1); },
    setDirty(value) { dirty = value; }, popBack,
    keepRoute() { history.pushState(null, "", selectedRoute); },
    canLeave, openRecord, openJob, openWeek, openDecision, openLead, go, goBack, closeDetail, cancelDetail, routeRecord,
  };
}
