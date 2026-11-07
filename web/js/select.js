// Selecting text from a terminal on a phone. xterm.js cannot drag-select by
// touch, so the text opens as plain text in a view over the terminal, where
// the phone's own selection works (handles, Copy). Agents and tmux bring their
// pane's history from the server; other terminals, their buffer.

import { $, el, glyph, G, toast } from './util.js';

let current = null; // { view, forget }

export function closeSelect() {
  current?.view.remove();
  current?.forget();
  current = null;
}

const copy = (text, what) =>
  navigator.clipboard
    .writeText(text)
    .then(() => toast(`${what} copied`))
    .catch(() => toast('Clipboard not available', { error: true }));

/**
 * @param {{text: string, load?: Promise<string>|null, fromBottom?: number}} opts
 *   text: shown at once; load: longer text that replaces it when it arrives;
 *   fromBottom: the row held, counted from the bottom of the screen.
 */
export function openSelect({ text, load = null, fromBottom = 0 }) {
  closeSelect();
  const pre = el('pre', { class: 'select-text', text: text || ' ' });
  const button = (icon, label, onClick, cls = '') => {
    const b = el('button', { type: 'button', class: `select-btn ${cls}`.trim() }, icon ? glyph(icon) : null, el('span', { text: label }));
    b.addEventListener('click', onClick);
    return b;
  };
  // The last selection in the text: on iOS a tap on a button can clear it first.
  let last = '';
  const onSelection = () => {
    const sel = window.getSelection();
    if (sel && pre.contains(sel.anchorNode) && sel.toString()) last = sel.toString();
  };
  document.addEventListener('selectionchange', onSelection);
  const view = el(
    'div',
    { class: 'select-view', role: 'dialog', 'aria-label': 'Select text' },
    el(
      'div',
      { class: 'select-bar' },
      button(null, 'Done', closeSelect, 'primary'),
      el('span', { class: 'select-hint', text: 'Hold to select' }),
      button(G.copy, 'Copy', () => {
        if (last) copy(last, 'Selection');
        else toast('Hold on the text to select some first');
      }),
      button(G.copy, 'All', () => copy(pre.textContent, 'All')),
    ),
    pre,
  );
  $('#app').append(view);
  current = { view, forget: () => document.removeEventListener('selectionchange', onSelection) };
  let touched = false; // once the user scrolls or selects, the text stays put
  pre.addEventListener('touchstart', () => (touched = true), { passive: true });
  const place = () => {
    const lh = parseFloat(getComputedStyle(pre).lineHeight) || 18;
    const target = pre.scrollHeight - pre.clientHeight / 2 - (fromBottom + 0.5) * lh;
    pre.scrollTop = Math.max(0, Math.min(pre.scrollHeight, target));
  };
  requestAnimationFrame(place);
  load
    ?.then((longer) => {
      if (current?.view !== view || touched || !longer || longer.length <= text.length) return;
      pre.textContent = longer;
      requestAnimationFrame(place);
    })
    .catch(() => {}); // the buffer is still there
}
