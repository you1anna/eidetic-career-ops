// Shared job-status buttons submit the revision of the record the user actually saw.
import {E, typeName} from './dom.js';
import {JOB_ACTION_NAMES} from './labels.js';
const pending = new Set();

export function jobStatusButton(record, to, {mutate, canLeave, saved, failed}, label = null, cls = '') {
  const button = E('button', label || JOB_ACTION_NAMES[to] || typeName(to), cls);
  button.type = 'button'; button.dataset.jobStatus = to;
  button.onclick = async () => {
    if (pending.has(record.id) || !canLeave()) return;
    pending.add(record.id); button.disabled = true;
    try {
      await mutate(`/api/records/${encodeURIComponent(record.id)}/transition`, {
        revision: record.revision, field: 'status', to,
      });
      saved(record.id, to);
    } catch (error) { failed(error.message); }
    finally { pending.delete(record.id); button.disabled = false; }
  };
  return button;
}
