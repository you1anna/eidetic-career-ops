// Browser copy of the person-queue sets in workspace/machines.py.
// workspace/test_machines.py fails if these lists drift.

export const PERSON_TERMINAL = ['research', 'held', 'no_response', 'closed', 'skipped', 'won', 'handed_over'];
export const NEEDS_REPLY = ['replied', 'conversation'];

const TERMINAL = new Set(PERSON_TERMINAL);
const REPLY = new Set(NEEDS_REPLY);

export function queueKey(progress) {
  const step = progress?.next_step || {};
  const text = typeof step.text === 'string' ? step.text.trim() : '';
  if (progress?.reply_to_record) return 'needs_reply';
  if (TERMINAL.has(progress?.status)) return '';
  if (step.kind === 'done') return '';
  if ((step.kind === 'mine' || step.kind === 'waiting') && text) return step.kind === 'mine' ? 'next_step' : 'waiting';
  if (REPLY.has(progress?.status)) return 'needs_reply';
  if (progress?.status === 'waiting') return 'waiting';
  return '';
}
