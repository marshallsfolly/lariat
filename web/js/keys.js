// The mobile key bar: Esc, Tab, sticky Ctrl/Alt, arrows with auto-repeat,
// a few symbols, the multiplexer prefix, PgUp/PgDn, paste, mic, hide keyboard.
//
// Buttons act on pointerdown and preventDefault() it, so xterm's hidden
// textarea keeps focus and the iOS keyboard stays up.

import { el, glyph, G, vibrate } from './util.js';

const ARROWS = { left: 'D', down: 'B', up: 'A', right: 'C' };

export class KeyBar {
  /**
   * @param {HTMLElement} host
   * @param {{send:(s:string)=>void, appCursor:()=>boolean, paste:()=>void,
   *          blur:()=>void, dictation:boolean, onMicDown:(btn:HTMLElement)=>void,
   *          onMicUp:(btn:HTMLElement)=>void}} hooks
   * @param {HTMLElement} [end] fixed slot after the scrolling keys (the mic)
   */
  constructor(host, hooks, end) {
    this.host = host;
    this.hooks = hooks;
    this.end = end;
    this.ctrl = 'off'; // off | once | lock
    this.alt = 'off';
    this.prefix = null;
    this.render();
  }

  setPrefix(bytes) {
    this.prefix = bytes || null;
    this.prefixBtn.hidden = !this.prefix;
    this.fade?.();
  }

  /** Apply sticky modifiers to text typed on the soft keyboard. */
  transform(data) {
    if (this.ctrl === 'off' && this.alt === 'off') return data;
    let out = data;
    if (this.ctrl !== 'off' && data.length === 1) out = ctrlChar(data);
    if (this.alt !== 'off') out = '\x1b' + out;
    this.release();
    return out;
  }

  release() {
    if (this.ctrl === 'once') this.ctrl = 'off';
    if (this.alt === 'once') this.alt = 'off';
    this.paintMods();
  }

  paintMods() {
    for (const [btn, state] of [
      [this.ctrlBtn, this.ctrl],
      [this.altBtn, this.alt],
    ]) {
      btn.classList.toggle('on', state === 'once');
      btn.classList.toggle('lock', state === 'lock');
    }
  }

  toggleMod(which) {
    const now = Date.now();
    const key = `${which}Tap`;
    const state = this[which];
    if (state === 'once' && now - (this[key] || 0) < 350) this[which] = 'lock';
    else this[which] = state === 'off' ? 'once' : 'off';
    this[key] = now;
    this.paintMods();
  }

  sendKey(seq) {
    const ctrl = this.ctrl !== 'off';
    const alt = this.alt !== 'off';
    let out = seq;
    if (ctrl && seq.length === 1) out = ctrlChar(seq);
    if (alt) out = '\x1b' + out;
    this.release();
    this.hooks.send(out);
  }

  arrow(dir) {
    const ctrl = this.ctrl !== 'off';
    const alt = this.alt !== 'off';
    const mod = 1 + (alt ? 2 : 0) + (ctrl ? 4 : 0);
    let seq;
    if (mod > 1) seq = `\x1b[1;${mod}${ARROWS[dir]}`;
    else seq = (this.hooks.appCursor() ? '\x1bO' : '\x1b[') + ARROWS[dir];
    this.release();
    this.hooks.send(seq);
  }

  button(label, { cls = 'key', aria, down, click, repeat = false }) {
    const btn = el('button', { class: cls, type: 'button', 'aria-label': aria || undefined }, label);
    let delay = 0;
    let rate = 0;
    const stop = () => {
      clearTimeout(delay);
      clearInterval(rate);
      btn.classList.remove('down');
    };
    btn.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      if (!down) return;
      btn.classList.add('down');
      vibrate(6);
      down();
      if (repeat) delay = setTimeout(() => (rate = setInterval(down, 60)), 400);
    });
    for (const ev of ['pointerup', 'pointercancel', 'pointerleave']) btn.addEventListener(ev, stop);
    if (click) btn.addEventListener('click', click);
    btn.addEventListener('contextmenu', (e) => e.preventDefault());
    return btn;
  }

  render() {
    const k = (label, seq, opts = {}) => this.button(label, { down: () => this.sendKey(seq), ...opts });
    const arrow = (dir) => this.button(glyph(G[dir]), { down: () => this.arrow(dir), repeat: true, aria: dir });
    const group = (...keys) => el('div', { class: 'kgroup' }, ...keys.filter(Boolean));
    this.ctrlBtn = this.button('ctrl', { cls: 'key mod', down: () => this.toggleMod('ctrl') });
    this.altBtn = this.button('alt', { cls: 'key mod', down: () => this.toggleMod('alt') });
    this.prefixBtn = this.button('pfx', {
      cls: 'key mod',
      aria: 'multiplexer prefix',
      down: () => this.prefix && this.hooks.send(this.prefix),
    });
    this.prefixBtn.hidden = !this.prefix;
    this.micBtn = null;
    if (this.hooks.dictation) {
      // Tap to start/stop, or hold to talk: the app decides on release.
      const mic = el('button', { class: 'mic', type: 'button', 'aria-label': 'dictate' }, glyph(G.micLine), el('span', { class: 'time' }));
      mic.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        vibrate(6);
        this.hooks.onMicDown(mic);
      });
      mic.addEventListener('pointerup', () => this.hooks.onMicUp(mic));
      mic.addEventListener('contextmenu', (e) => e.preventDefault());
      this.micBtn = mic;
    }
    this.host.replaceChildren(
      group(k('esc', '\x1b', { cls: 'key mod' }), k('tab', '\t', { cls: 'key mod' })),
      group(this.ctrlBtn, this.altBtn, this.prefixBtn),
      group(arrow('left'), arrow('down'), arrow('up'), arrow('right')),
      group(k('^C', '\x03', { cls: 'key mod', aria: 'control C' })),
      group(k('|', '|'), k('~', '~'), k('/', '/'), k('-', '-'), k('_', '_')),
      group(
        k(glyph(G.pgUp), '\x1b[5~', { repeat: true, aria: 'page up' }),
        k(glyph(G.pgDn), '\x1b[6~', { repeat: true, aria: 'page down' }),
      ),
      group(
        this.button(glyph(G.clip), { aria: 'paste', click: () => this.hooks.paste() }),
        this.button(glyph(G.kbdHide), { aria: 'hide keyboard', click: () => this.hooks.blur() }),
      ),
    );
    // Enter stays in reach next to the mic, whatever the keys are scrolled to.
    const enter = k(glyph(G.enter), '\r', { cls: 'key enter', aria: 'enter' });
    // An image for the agent: the picker needs the tap itself, so this is a click.
    const image = this.button(glyph(G.image), { cls: 'key image', aria: 'attach an image', click: () => this.hooks.attach() });
    this.end?.replaceChildren(group(image, enter), ...[this.micBtn].filter(Boolean));
    this.paintMods();
    // Fade whichever edge has more keys behind it.
    const fade = () => {
      const h = this.host;
      h.classList.toggle('more-left', h.scrollLeft > 2);
      h.classList.toggle('more-right', h.scrollLeft + h.clientWidth < h.scrollWidth - 2);
    };
    this.fade = fade;
    this.host.onscroll = fade;
    requestAnimationFrame(fade);
    if (!this.resizeWatch) {
      this.resizeWatch = new ResizeObserver(fade);
      this.resizeWatch.observe(this.host);
    }
  }
}

function ctrlChar(ch) {
  const c = ch.toLowerCase();
  if (c >= 'a' && c <= 'z') return String.fromCharCode(c.charCodeAt(0) - 96);
  const table = { '[': '\x1b', '\\': '\x1c', ']': '\x1d', '^': '\x1e', _: '\x1f', '/': '\x1f', ' ': '\x00', '@': '\x00', '?': '\x7f' };
  return table[c] ?? ch;
}
