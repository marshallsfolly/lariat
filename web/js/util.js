// Small DOM helpers and the Nerd Font glyphs the UI uses.

/** Nerd Font codepoints (present in every Nerd Fonts v3 patched font). */
export const G = {
  robot: '\u{f06a9}',
  server: '󰒋',
  update: '󰚰',
  select: '󰗧',
  download: '󰇚',
  term: '',
  tmux: '',
  bell: '',
  check: '',
  plus: '',
  keyboard: '',
  mic: '',
  paste: '',
  copy: '',
  warn: '',
  eye: '',
  pencil: '',
  trash: '',
  close: '',
  refresh: '',
  signout: '',
  folder: '',
  send: '',
  moon: '',
  hourglass: '',
  question: '',  left: '󰁍',
  down: '󰁅',
  up: '󰁝',
  right: '󰁔',
  pgUp: '󰄿',
  pgDn: '󰄼',
  kbdHide: '󰌏',
  clip: '󰆒',
  enter: '󰌑',
  timer: '󰔛',
  textSize: '󰉿',
  minus: '󰍴',
  plusBox: '󰐕',
  micLine: '󰍬',
  monitor: '󰍹',  image: '󰋩',
};

export const $ = (sel, root = document) => root.querySelector(sel);

/**
 * el('div', {class: 'x', onclick: fn}, child, 'text', ...)
 * Attributes starting with "on" become listeners; `dataset` and `hidden` are handled.
 */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, String(v));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const NUMBERS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine'];
const SOUNDS_LIKE = { to: '2', too: '2', won: '1', for: '4', fore: '4', ate: '8' };
const LEADS = ['option', 'number', 'choice', 'press', 'pick', 'select'];

/**
 * A dictation that is only a number ("Zero.", "two", "0", "option two") is a menu choice
 * ("1 to review · 0 to dismiss", "1. Yes 2. No"): that key, pressed. A paste
 * of "0" lands in the prompt as text instead. Null for anything else.
 */
export function spokenKey(text) {
  let words = String(text).toLowerCase().replace(/[^a-z0-9 ]/g, ' ').split(/\s+/).filter(Boolean);
  if (words.length === 2 && LEADS.includes(words[0])) words = words.slice(1); // "option two": clearer to say, and to hear
  if (words.length !== 1) return null;
  const t = words[0];
  if (/^[0-9]$/.test(t)) return t;
  const i = NUMBERS.indexOf(t);
  if (i >= 0) return String(i);
  return SOUNDS_LIKE[t] ?? null; // what Whisper writes for a lone "two", "one", "four", "eight"
}

export const glyph = (g, cls = '') => el('span', { class: `nf ${cls}`.trim(), 'aria-hidden': 'true' }, g);

let toastTimer = 0;
export function toast(message, { error = false, ms = 2600, onClick = null } = {}) {
  const t = $('#toast');
  t.textContent = message;
  t.classList.toggle('error', error);
  t.classList.add('show');
  t.onclick = onClick;
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(() => t.classList.remove('show'), ms);
}

export function ago(seconds) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - seconds));
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

/** Last n non-empty lines of a block of text. */
export function tail(text, n) {
  const lines = (text || '').split('\n').map((l) => l.replace(/\s+$/, '')).filter((l) => l.trim());
  return lines.slice(-n).join('\n');
}

export function storage(key, fallback) {
  try {
    const v = localStorage.getItem(`lariat.${key}`);
    return v === null ? fallback : JSON.parse(v);
  } catch {
    return fallback;
  }
}

export function store(key, value) {
  try {
    localStorage.setItem(`lariat.${key}`, JSON.stringify(value));
  } catch {
    /* private mode or full: the setting just isn't remembered */
  }
}

export function vibrate(ms = 10) {
  try {
    navigator.vibrate?.(ms);
  } catch {
    /* not supported on iOS; harmless */
  }
}
