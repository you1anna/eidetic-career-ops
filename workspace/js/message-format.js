// The one reading format for an outreach note. The stored text stays the source: the note box,
// Copy and the sent snapshot all keep it. Preview and View sent message render this subset.
// **phrase** is emphasis. A run of "- ", "* " or "• " lines is a bullet list. A run of "1. " lines
// is a numbered list. A blank line separates paragraphs. Nothing else is markup.

const bold = () => /\*\*([^*\n]+)\*\*/g;
const HAS_BOLD = /\*\*[^*\n]+\*\*/;
const LIST_LINE = /^[ \t]*(?:[-*•]|\d+\.)[ \t]+\S/m;
const BULLET = /^[ \t]*[-*•][ \t]+(.*)$/;
const NUMBER = /^[ \t]*\d+\.[ \t]+(.*)$/;

export function structured(text) {
  const source = String(text || '');
  return HAS_BOLD.test(source) || LIST_LINE.test(source);
}

// The wording Copy sends. Emphasis marks come off; list lines and paragraph breaks stay,
// so a plain-text paste still reads as a list.
export function reading(text) {
  return String(text || '').replace(bold(), '$1');
}

export function blocks(text) {
  const lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let paragraph = [];
  const flush = () => {
    if (!paragraph.length) return;
    out.push({ type: 'p', text: paragraph.join('\n') });
    paragraph = [];
  };
  for (let i = 0; i < lines.length; i++) {
    if (!lines[i].trim()) { flush(); continue; }
    const bullet = BULLET.exec(lines[i]);
    const number = NUMBER.exec(lines[i]);
    if (!bullet && !number) { paragraph.push(lines[i]); continue; }
    flush();
    const kind = bullet ? 'ul' : 'ol';
    const pattern = bullet ? BULLET : NUMBER;
    const items = [];
    for (; i < lines.length; i++) {
      const item = pattern.exec(lines[i]);
      if (!item) { i--; break; }
      items.push(item[1]);
    }
    out.push({ type: kind, items });
  }
  flush();
  return out;
}

const escapeHtml = text => String(text).replace(/[&<>"]/g, ch => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
}[ch]));

function inlineHtml(text) {
  let out = '';
  let last = 0;
  for (const mark of String(text).matchAll(bold())) {
    out += escapeHtml(text.slice(last, mark.index));
    out += `<strong>${escapeHtml(mark[1])}</strong>`;
    last = mark.index + mark[0].length;
  }
  return out + escapeHtml(text.slice(last)).replace(/\n/g, '<br>');
}

// HTML for a rich paste. Every character from the note is escaped; only this format adds tags.
export function readingHtml(text) {
  return blocks(text).map(block => (block.type === 'p'
    ? `<p>${inlineHtml(block.text)}</p>`
    : `<${block.type}>${block.items.map(item => `<li>${inlineHtml(item)}</li>`).join('')}</${block.type}>`)).join('');
}

function appendInline(node, text) {
  let last = 0;
  for (const mark of String(text).matchAll(bold())) {
    if (mark.index > last) node.append(text.slice(last, mark.index));
    const strong = document.createElement('strong');
    strong.textContent = mark[1];
    node.append(strong);
    last = mark.index + mark[0].length;
  }
  if (last < text.length) node.append(text.slice(last));
}

// Plain text always. A structured note also offers HTML when the clipboard can take it.
export async function copyMessage(text) {
  const plain = reading(text);
  try {
    if (structured(text) && typeof ClipboardItem !== 'undefined' && navigator.clipboard?.write) {
      await navigator.clipboard.write([new ClipboardItem({
        'text/plain': new Blob([plain], { type: 'text/plain' }),
        'text/html': new Blob([readingHtml(text)], { type: 'text/html' }),
      })]);
      return true;
    }
    await navigator.clipboard.writeText(plain);
    return true;
  } catch {
    return false;
  }
}

export function renderMessage(parent, text) {
  const source = String(text || '');
  if (!structured(source)) {
    parent.classList.add('is-plain');
    parent.textContent = source;
    return;
  }
  parent.classList.remove('is-plain');
  parent.replaceChildren(...blocks(source).map(block => {
    if (block.type === 'p') {
      const paragraph = document.createElement('p');
      appendInline(paragraph, block.text);
      return paragraph;
    }
    const list = document.createElement(block.type);
    block.items.forEach(item => {
      const line = document.createElement('li');
      appendInline(line, item);
      list.append(line);
    });
    return list;
  }));
}
