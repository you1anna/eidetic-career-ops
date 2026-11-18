// The application owns requests and DOM updates; this rule keeps old replies and edits safe.
export function summaryRefreshDecision({baseVersion, currentVersion, nextVersion, editing = false}) {
  if (baseVersion !== currentVersion) return 'discard';
  if (nextVersion && nextVersion === currentVersion) return 'unchanged';
  return editing ? 'pending' : 'apply';
}

// Preserve reader context for a background-only redraw. Editors and open sheets never reach here.
export function retainView(root) {
  const identity = node => node.dataset.persistKey || node.id ||
    `${node.closest('[data-opportunity-id],[data-action-id]')?.dataset.opportunityId || node.closest('[data-action-id]')?.dataset.actionId || 'page'}:${node.className || node.querySelector(':scope > summary')?.textContent || ''}`;
  const opened = new Set([...root.querySelectorAll('details[open]')].map(identity));
  const active = document.activeElement;
  const owner = active?.closest('[data-opportunity-id],[data-action-id]');
  const focus = active && root.contains(active) ? {id:active.id, owner:owner?.dataset.opportunityId,
    action:owner?.dataset.actionId, name:active.getAttribute('name'), job:active.dataset.jobStatus,
    tag:active.tagName.toLowerCase(), cls:[...active.classList].join('.')} : null;
  const x = window.scrollX, y = window.scrollY;
  const anchor = [...root.querySelectorAll('[data-opportunity-id]')].find(node => node.getBoundingClientRect().bottom > 0);
  const anchorId = anchor?.dataset.opportunityId, anchorTop = anchor?.getBoundingClientRect().top;
  return next => {
    for (const node of next.querySelectorAll('details')) if (opened.has(identity(node))) node.open = true;
    requestAnimationFrame(() => {
      if (focus) {
        const scope = focus.owner ? next.querySelector(`[data-opportunity-id="${CSS.escape(focus.owner)}"]`)
          : focus.action ? next.querySelector(`[data-action-id="${CSS.escape(focus.action)}"]`) : next;
        const selector = focus.id ? `#${CSS.escape(focus.id)}` : focus.job ? `[data-job-status="${CSS.escape(focus.job)}"]`
          : focus.name ? `[name="${CSS.escape(focus.name)}"]` : focus.tag + (focus.cls ? '.' + focus.cls.split('.').map(CSS.escape).join('.') : '');
        scope?.querySelector(selector)?.focus({preventScroll:true});
      }
      const nextAnchor = anchorId && next.querySelector(`[data-opportunity-id="${CSS.escape(anchorId)}"]`);
      window.scrollTo(x, nextAnchor ? window.scrollY + nextAnchor.getBoundingClientRect().top - anchorTop : y);
    });
  };
}
