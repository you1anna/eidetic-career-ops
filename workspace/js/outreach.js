// Canonical action lookup for record sheets and status labels. Work admission belongs to
// state.opportunities; this module never selects, scores or orders work.
export function createOutreachViews({ getState, getRecord }) {
  let actions, research, byRecord;
  const candidates = id => {
    const state = getState();
    if (!byRecord || actions !== state.intelligence?.actions || research !== state.intelligence?.research) {
      byRecord = new Map();
      actions = state.intelligence?.actions; research = state.intelligence?.research;
      const add = (id, action) => {
        if (!byRecord.has(id)) byRecord.set(id, []);
        byRecord.get(id).push(action);
      };
      // Preserve actions-before-research order and repeated entries. One entry that matches
      // both its record and agency still appears once, as with the original filter.
      for (const collection of [actions, research]) for (const action of collection || []) {
        add(action.record_id, action);
        if (action.route === 'agency' && action.company_id !== action.record_id) add(action.company_id, action);
      }
    }
    return byRecord.get(id) || [];
  };
  const demoted = record => record?.type === 'person' && record.relationship === 'do_not_contact';
  const excluded = action => !!(action.exclusion || getRecord(action.company_id)?.exclusion ||
    getRecord(action.record_id)?.exclusion || getRecord(action.record_id)?.exclusion_contexts?.length ||
    demoted(getRecord(action.record_id)) || demoted(getRecord(action.person_id)));
  const forRecord = record => candidates(record.id).filter(action =>
    (action.record_id === record.id || (record.type === 'agency' && action.company_id === record.id && action.route === 'agency')) &&
    !excluded(action));
  return { forRecord };
}
