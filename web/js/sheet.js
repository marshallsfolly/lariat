// Bottom sheets: action tiles, grouped lists, settings (switches, choices,
// steppers), forms and hold-to-confirm buttons.

import { $, el, glyph, G, vibrate } from './util.js';

let current = null;

export function closeSheet() {
  if (!current) return;
  const { wrap, onClose } = current;
  current = null;
  wrap.classList.remove('open');
  const done = () => wrap.remove();
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) done();
  else setTimeout(done, 170);
  onClose?.();
}

/**
 * openSheet({title, sub, body: Node[], onClose}) -> close()
 */
export function openSheet({ title, sub, body = [], onClose }) {
  closeSheet();
  const sheet = el(
    'div',
    { class: 'sheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': title || 'menu' },
    el('div', { class: 'grab' }),
    title ? el('h2', { text: title }) : null,
    sub ? el('p', { class: 'sub', text: sub }) : null,
    ...body,
  );
  const backdrop = el('div', { class: 'sheet-backdrop', onclick: closeSheet });
  const wrap = el('div', { class: 'sheet-wrap' }, backdrop, sheet);
  $('#sheet-root').append(wrap);
  current = { wrap, onClose };
  // Start the slide on the next frame so the transition runs.
  requestAnimationFrame(() => requestAnimationFrame(() => wrap.classList.add('open')));
  return closeSheet;
}

export const sheetOpen = () => current !== null;

/**
 * A sheet row. {icon, label, danger, hold, onSelect}
 * hold: the action fires only after a 1 s press (destructive actions).
 */
export function item({ icon, label, danger = false, hold = false, onSelect, close = true }) {
  const fill = hold ? el('span', { class: 'fill' }) : null;
  const button = el(
    'button',
    { class: `item${danger ? ' danger' : ''}`, type: 'button' },
    fill,
    glyph(icon || ''),
    el('span', { text: hold ? `${label} (hold)` : label }),
  );
  const fire = () => {
    if (close) closeSheet();
    onSelect?.();
  };
  if (!hold) {
    button.addEventListener('click', fire);
    return button;
  }
  let timer = 0;
  const cancel = () => {
    clearTimeout(timer);
    button.classList.remove('holding');
  };
  button.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    button.classList.add('holding');
    timer = setTimeout(() => {
      button.classList.remove('holding');
      vibrate(20);
      fire();
    }, 1000);
  });
  for (const ev of ['pointerup', 'pointercancel', 'pointerleave']) button.addEventListener(ev, cancel);
  button.addEventListener('contextmenu', (e) => e.preventDefault());
  return button;
}

export const items = (...rows) => el('div', { class: 'items' }, ...rows.filter(Boolean));

/** A sheet's main actions as big icon tiles. */
export const tiles = (...list) => el('div', { class: 'tiles' }, ...list.filter(Boolean));

export function tile({ icon, label, onSelect, close = true }) {
  const button = el('button', { class: 'tile', type: 'button' }, glyph(icon), el('span', { text: label }));
  button.addEventListener('click', () => {
    if (close) closeSheet();
    onSelect?.();
  });
  return button;
}

/** A titled group of setting rows. */
export const section = (title, ...rows) =>
  el('section', { class: 'sheet-section' }, el('h3', { text: title }), el('div', { class: 'group' }, ...rows.filter(Boolean)));

/** A setting with a switch; the sheet stays open. If onChange rejects, it flips back. */
export function toggle({ icon, label, value, onChange }) {
  let on = !!value;
  const row = el(
    'button',
    { class: 'setting', type: 'button', role: 'switch', 'aria-checked': String(on) },
    glyph(icon),
    el('span', { class: 'label', text: label }),
    el('span', { class: 'switch', 'aria-hidden': 'true' }),
  );
  const paint = () => row.setAttribute('aria-checked', String(on));
  row.addEventListener('click', () => {
    on = !on;
    paint();
    vibrate(6);
    const result = onChange(on);
    result?.catch?.(() => {
      on = !on;
      paint();
    });
  });
  return row;
}

/** A line of explanation inside a section. */
export const note = (text) => el('p', { class: 'setting note', text });

/** A setting with a segmented choice. options: [[value, label], ...] */
export function choice({ icon, label, options, value, onChange }) {
  const buttons = options.map(([v, text]) => {
    const b = el('button', { type: 'button', class: v === value ? 'on' : '', text });
    b.addEventListener('click', () => {
      for (const x of buttons) x.classList.toggle('on', x === b);
      vibrate(6);
      onChange(v);
    });
    return b;
  });
  return el(
    'div',
    { class: 'setting stacked' },
    el('div', { class: 'setting-head' }, glyph(icon), el('span', { class: 'label', text: label })),
    el('div', { class: 'seg' }, ...buttons),
  );
}

/** A setting with − value + buttons. onStep(delta) returns the new value. */
export function stepper({ icon, label, value, onStep }) {
  const out = el('span', { class: 'value', text: String(value) });
  const step = (g, delta, aria) =>
    el('button', { class: 'step', type: 'button', 'aria-label': aria, onclick: () => (out.textContent = String(onStep(delta))) }, glyph(g));
  return el(
    'div',
    { class: 'setting' },
    glyph(icon),
    el('span', { class: 'label', text: label }),
    el('div', { class: 'stepper' }, step(G.minus, -1, 'smaller'), out, step(G.plusBox, 1, 'larger')),
  );
}

export function field(label, input) {
  return el('label', { class: 'field' }, el('span', { text: label }), input);
}
