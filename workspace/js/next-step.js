// One form for recording or changing a human commitment on a specific approach.
import { E, A } from './dom.js';

export function nextStepFields(current, { keep = false } = {}) {
  const field = (title, input) => {
    input.setAttribute('aria-label', title);
    return A(E('label', null, 'field span-2'), E('span', title), input);
  };
  const choice = E('select'); choice.name = 'next_step_choice';
  const choices = [...(keep ? [['keep', current && ['mine','waiting'].includes(current.kind) ? 'Keep the current next step' : 'No new next step']] : []),
    ['mine', 'I need to act'], ['waiting', 'Waiting for them'], ['done', 'The next step is complete'],
    ['none', current ? 'Clear the recorded next step' : 'No next step set']];
  choices.forEach(([value, label]) => {const o = E('option', label); o.value = value; choice.append(o);});
  choice.value = keep ? 'keep' : current?.kind || 'none';
  const body = E('textarea'); body.name = 'next_step_text'; body.rows = 2; body.maxLength = 500;
  body.value = current?.text || ''; body.placeholder = 'For example: send the platform recovery example';
  const date = E('input'); date.type = 'date'; date.name = 'next_step_agreed'; date.value = current?.agreed_on || '';
  const textField = field('Actual next step', body), dateField = field('Agreed date (optional)', date);
  const help = E('p', 'Only a date actually agreed or promised. No reminder is scheduled.', 'muted span-2');
  const kept = E('p', current?.text ? `Current: ${current.text}` : 'Contact can be recorded without inventing a next step.', 'muted span-2');
  const box = A(E('div', null, 'form-grid next-step-fields span-2'), field('What happens next?', choice), kept, textField, dateField, help);
  function update() {
    const active = ['mine','waiting'].includes(choice.value);
    kept.hidden = choice.value !== 'keep';
    textField.hidden = dateField.hidden = help.hidden = !active;
    body.disabled = date.disabled = !active; body.required = active;
    if (choice.value === 'waiting' && !body.value.trim()) body.value = 'Wait for a reply';
  }
  choice.onchange = update; update();
  return { box, read: () => choice.value === 'keep' ? undefined : {
    kind:choice.value, text:body.value, agreed_on:['mine','waiting'].includes(choice.value) ? date.value : '',
  } };
}
