import { E, A, extLink } from './dom.js';
import { api } from './api.js';

const when = value => value ? new Date(value).toLocaleString('en-GB', {day:'numeric', month:'short', year:'numeric'}) : 'Date unknown';

export function createEmailLeads({ navigation, load, getRecord, showNotice, elements }) {
  let panelNode, results, message, searchInput, sourceInput, request = 0, opened = false;
  function panel() {
    if (panelNode) return panelNode;
    panelNode = A(E('details', null, 'email-leads'), E('summary', 'Review unassessed email leads'));
    panelNode.dataset.emailLeads = '';
    panelNode.append(E('p', 'Recent email leads not yet saved to Jobs. Email claims are unverified; opening a lead makes no changes.', 'muted'));
    const form = E('form', null, 'lead-search');
    searchInput = E('input'); searchInput.type = 'search'; searchInput.maxLength = 200;
    searchInput.placeholder = 'Company or job title'; searchInput.setAttribute('aria-label', 'Search email leads');
    sourceInput = E('select'); sourceInput.setAttribute('aria-label', 'Email source');
    const all = E('option', 'All email sources'); all.value = ''; sourceInput.append(all);
    const submit = E('button', 'Search'); submit.type = 'submit';
    form.append(searchInput, sourceInput, submit);
    form.onsubmit = event => { event.preventDefault(); read(); };
    message = E('p', '', 'muted'); message.setAttribute('role', 'status');
    results = E('div', null, 'lead-results');
    panelNode.append(form, message, results);
    panelNode.ontoggle = () => { if (panelNode.open && !opened) { opened = true; read(); } };
    return panelNode;
  }
  async function read() {
    const token = ++request;
    message.textContent = 'Loading email leads…'; results.replaceChildren();
    try {
      const data = await api('/api/email-leads?' + new URLSearchParams({q:searchInput.value, source:sourceInput.value}));
      if (token !== request) return;
      if (sourceInput.options.length === 1) for (const source of data.sources) {
        const option = E('option', source.label); option.value = source.id; sourceInput.append(option);
      }
      message.textContent = data.items.length ? `Newest first · showing up to ${data.limit} matching leads. Search to find older leads.` : 'No matching unsaved leads. Saved roles are in Jobs.';
      for (const row of data.items) {
        const link = E('a', 'Review'); link.href = row.review_url;
        const entry = A(E('article', null, 'email-lead-row'), E('span', 'Unassessed', 'chip needs-review'),
          E('strong', `${row.company || 'Employer not stated'} · ${row.title}`),
          E('small', `${row.source_label} · ${when(row.received_at)}`, 'muted'), link);
        results.append(entry);
      }
    } catch (error) { if (token === request) message.textContent = `${error.message} Use Search to retry.`; }
  }
  async function show(id) {
    const {dialog, content, kind} = elements;
    kind.textContent = 'Email lead · Unassessed';
    content.replaceChildren(E('h2', 'Loading email lead…'));
    if (!dialog.open) dialog.showModal();
    try {
      const lead = await api(`/api/email-leads/${id}`);
      if (navigation.leadId !== id || !dialog.open) return;
      const title = E('h2', `${lead.company || 'Employer not stated'} · ${lead.title}`); title.id = 'detail-title';
      const context = A(E('section', null, 'lead-email-context'), E('h3', 'Email says — unverified'),
        E('p', `${lead.source_label} · ${when(lead.received_at)}`),
        E('p', `Pay: ${lead.pay_text || 'Not stated in the email'} · Location: ${lead.location || 'Not stated in the email'}`));
      const summary = A(E('details'), E('summary', 'Original email summary'),
        E('p', lead.email_summary || 'No separate summary was retained. Open the original email for its wording.', 'lead-original-summary'));
      context.append(summary);
      const links = A(E('div', null, 'job-links'), extLink(lead.url, 'Open source advert ↗'));
      if (lead.original_email) links.append(extLink(lead.original_email, 'Find original email in Gmail ↗'));
      if (lead.email_available) {
        const original = E('a', 'Download original email'); original.href = `/api/email-leads/${id}/original`; original.download = 'original-alert.eml'; links.append(original);
      } else context.append(E('p', 'Original email unavailable in the retained spool. Check your mail app.', 'muted'));
      const review = E('a', 'Review link'); review.href = lead.review_url;
      links.append(review); context.append(links);
      content.replaceChildren(title, E('p', 'Unassessed — this email is a lead, not a researched recommendation.', 'notice'), context);
      if (!lead.eligible) content.append(E('p', 'This lead no longer passes intake filters or its source is closed. Review the source before saving it manually in Jobs.', 'muted'));
      if (lead.record_id) {
        const existing = E('button', 'Open saved role'); existing.onclick = () => navigation.openRecord(lead.record_id);
        content.append(E('p', 'This lead already has a record in Jobs. Its existing status is preserved.'), existing);
      }
      if ((!lead.record_id || [null, '', 'candidate'].includes(lead.record_status)) && lead.eligible) {
        const save = E('button', 'Shortlist', 'primary'); save.type = 'button';
        const status = E('p', '', 'muted'); status.setAttribute('role', 'status');
        content.append(E('p', 'Shortlisting adds this role to Act now and bypasses the automatic intake limit. It does not verify the advert or record an application.'), save, status);
        save.onclick = async () => {
          save.disabled = true; status.textContent = 'Saving shortlist…';
          let saved;
          try {
            saved = await api(`/api/email-leads/${id}/shortlist`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({confirm:true})});
            await load('save');
            if (getRecord(saved.record.id)?.status !== saved.record.status) throw new Error('The saved role is not yet available in this page.');
            if (navigation.leadId !== id) { showNotice('Shortlist saved.'); return; }
            navigation.go('week'); navigation.openRecord(saved.record.id);
            showNotice('Shortlisted. Email claims remain unverified.');
            if (opened) read();
          } catch (error) {
            status.textContent = saved ? `Shortlist saved, but the page could not refresh. Reload to open the saved role. ${error.message}` : error.message;
            save.disabled = false;
          }
        };
      }
    } catch (error) {
      if (navigation.leadId !== id || !dialog.open) return;
      const retry = E('button', 'Retry'); retry.onclick = () => show(id);
      content.replaceChildren(E('h2', 'Could not open email lead'), E('p', error.message), retry);
    }
  }
  return {panel, show};
}
