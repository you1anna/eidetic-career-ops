// One observer for company images. The server owns company identity and allowed source URLs.
import {E} from './dom.js';

export function companyInitials(name) {
  return String(name || '').trim().split(/\s+/).filter(Boolean).slice(0, 2)
    .map(word => Array.from(word)[0]).join('').toLocaleUpperCase('en-GB') || '—';
}

export function createCompanyLogos() {
  let observer;
  const pending = new Map();
  const load = node => {
    const url = pending.get(node);
    if (!url) return;
    pending.delete(node); observer?.unobserve(node);
    node.src = url;
  };
  function reset() { observer?.disconnect(); pending.clear(); }
  function logo(name, source) {
    const box = E('span', null, 'company-logo'); box.setAttribute('aria-hidden', 'true');
    const initials = E('span', companyInitials(name), 'company-initials'); box.append(initials);
    if (!source?.url) return box;
    const img = E('img'); img.alt = ''; img.width = 64; img.height = 64;
    img.referrerPolicy = 'no-referrer'; img.decoding = 'async'; img.loading = 'lazy';
    img.addEventListener('load', () => { img.classList.add('is-loaded'); initials.hidden = true; });
    img.addEventListener('error', () => { img.remove(); initials.hidden = false; });
    box.append(img);
    // Set no remote src before intersection: native lazy loading may fetch near the viewport.
    if (typeof IntersectionObserver === 'function') {
      observer ||= new IntersectionObserver(entries => {
        for (const entry of entries) if (entry.isIntersecting) load(entry.target);
      });
      pending.set(img, source.url); observer.observe(img);
    }
    // Unsupported observers retain initials rather than fetching every off-screen logo.
    return box;
  }
  return {logo, reset};
}
