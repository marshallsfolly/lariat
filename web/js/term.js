// The terminal view: xterm.js on a WebSocket to /ws/term.
// Binary frames are terminal bytes both ways; text frames are JSON control.

import { at } from './api.js';
import { Terminal } from '/vendor/xterm.mjs';
import { FitAddon } from '/vendor/addon-fit.mjs';
import { WebglAddon } from '/vendor/addon-webgl.mjs';
import { Unicode11Addon } from '/vendor/addon-unicode11.mjs';
import { WebLinksAddon } from '/vendor/addon-web-links.mjs';
import { BUILD } from './build-info.js';
import { storage, store } from './util.js';

const enc = new TextEncoder();
// Targets that survive a disconnect (reattaching shows the same screen).
const PERSISTENT = new Set(['agent', 'tmux', 'herdr']);

export class TermView {
  /**
   * @param {HTMLElement} host
   * @param {{k:string,t?:string,s?:string}} target
   * @param {{onStatus:(s:string,msg?:string)=>void, onExpired:()=>void, transform:(d:string)=>string}} hooks
   */
  constructor(host, target, hooks) {
    this.host = host;
    this.target = target;
    this.hooks = hooks;
    this.ws = null;
    this.status = 'connecting';
    this.retry = 0;
    this.retryTimer = 0;
    this.fitTimer = 0;
    this.disposed = false;
    this.size = { cols: 0, rows: 0 };
  }

  async open() {
    // Measure with the real font, or the cell grid is wrong and shifts later.
    try {
      await Promise.all([document.fonts.load('16px "lariat-mono"'), document.fonts.load('bold 16px "lariat-mono"')]);
    } catch {
      /* fall back to whatever is loaded */
    }
    if (this.disposed) return;
    const term = new Terminal({
      fontFamily: BUILD.fontFamily,
      fontSize: storage('fontSize', 14),
      lineHeight: 1.1,
      cursorStyle: 'block',
      cursorBlink: false,
      allowProposedApi: true,
      scrollback: 5000,
      macOptionIsMeta: true,
      theme: BUILD.theme,
      drawBoldTextInBrightColors: false,
      fontWeightBold: BUILD.fontBold ? 'bold' : 'normal',
      allowTransparency: false,
      smoothScrollDuration: 0,
    });
    this.term = term;
    this.fit = new FitAddon();
    term.loadAddon(this.fit);
    const unicode = new Unicode11Addon();
    term.loadAddon(unicode);
    term.unicode.activeVersion = '11';
    term.loadAddon(new WebLinksAddon((_e, uri) => window.open(uri, '_blank', 'noopener,noreferrer')));
    term.open(this.host);
    // DOM rendering is the default: it is verified everywhere and plenty fast for a
    // phone-sized grid. WebGL is opt-in from the terminal menu.
    if (storage('renderer', 'dom') === 'webgl') this.useWebgl();
    term.onData((d) => this.send(enc.encode(this.hooks.transform(d))));
    term.onBinary((d) => this.send(Uint8Array.from(d, (c) => c.charCodeAt(0) & 0xff)));
    this.touch();
    this.fitNow();
    this.connect();
    this.onVisible = () => {
      if (document.visibilityState === 'visible' && this.status !== 'live' && this.status !== 'exited') this.reconnect();
    };
    document.addEventListener('visibilitychange', this.onVisible);
  }

  useWebgl() {
    try {
      const webgl = new WebglAddon();
      webgl.onContextLoss(() => webgl.dispose()); // falls back to the DOM renderer
      this.term.loadAddon(webgl);
    } catch {
      /* no WebGL: the DOM renderer is fine */
    }
  }

  // -- size -------------------------------------------------------------

  scheduleFit() {
    clearTimeout(this.fitTimer);
    this.fitTimer = setTimeout(() => this.fitNow(), 50);
  }

  fitNow() {
    if (!this.term || this.disposed) return;
    try {
      this.fit.fit();
    } catch {
      return;
    }
    const { cols, rows } = this.term;
    if (cols !== this.size.cols || rows !== this.size.rows) {
      this.size = { cols, rows };
      this.control({ t: 'resize', cols, rows });
    }
  }

  setFontSize(px) {
    const size = Math.max(9, Math.min(28, Math.round(px)));
    if (size === this.term.options.fontSize) return;
    this.term.options.fontSize = size;
    store('fontSize', size);
    this.fitNow();
  }

  // -- socket -----------------------------------------------------------

  url() {
    const q = new URLSearchParams({ k: this.target.k, c: String(this.term.cols), r: String(this.term.rows) });
    if (this.target.t) q.set('t', this.target.t);
    if (this.target.s) q.set('s', this.target.s);
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}${at(this.target.m, '/ws/term')}?${q}`;
  }

  setStatus(status, msg) {
    this.status = status;
    this.hooks.onStatus(status, msg);
  }

  connect() {
    if (this.disposed) return;
    clearTimeout(this.retryTimer);
    this.setStatus('connecting');
    const ws = new WebSocket(this.url());
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    let exited = null;
    ws.onopen = () => {
      this.retry = 0;
      this.size = { cols: this.term.cols, rows: this.term.rows };
    };
    ws.onmessage = (e) => {
      if (typeof e.data === 'string') {
        let msg;
        try {
          msg = JSON.parse(e.data);
        } catch {
          return;
        }
        if (msg.t === 'hello') this.setStatus('live');
        else if (msg.t === 'exit') exited = msg.code;
        return;
      }
      this.term.write(new Uint8Array(e.data));
    };
    ws.onclose = (e) => {
      if (this.ws !== ws || this.disposed) return;
      this.ws = null;
      if (e.code === 4401) {
        this.setStatus('offline', 'Session expired');
        this.hooks.onExpired();
      } else if (exited !== null) {
        this.setStatus('exited', exited === 0 ? 'Exited' : `Exited (${exited})`);
      } else if (e.code === 4408) {
        this.setStatus('offline', 'Closed after an hour idle');
      } else if (PERSISTENT.has(this.target.k)) {
        this.retry = Math.min(this.retry + 1, 5);
        this.setStatus('offline', 'Disconnected, reconnecting…');
        this.retryTimer = setTimeout(() => this.reconnect(), 400 * 2 ** this.retry);
      } else {
        this.setStatus('offline', 'Disconnected');
      }
    };
  }

  reconnect() {
    if (this.disposed) return;
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
    // Attach clients redraw the whole screen; start from a clean one.
    this.term.reset();
    this.connect();
  }

  send(bytes) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(bytes);
  }

  sendText(text) {
    this.send(enc.encode(text));
  }

  control(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  // -- input helpers ----------------------------------------------------

  paste(text) {
    this.term.paste(text); // honours bracketed paste mode
  }

  focus() {
    this.term?.focus();
  }

  blur() {
    this.term?.blur();
    this.host.querySelector('textarea')?.blur();
  }

  appCursor() {
    return !!this.term?.modes?.applicationCursorKeysMode;
  }

  screenText() {
    const buf = this.term.buffer.active;
    const lines = [];
    for (let y = buf.viewportY; y < buf.viewportY + this.term.rows; y++) {
      lines.push(buf.getLine(y)?.translateToString(true) ?? '');
    }
    return lines.join('\n').replace(/\s+$/, '');
  }

  /** Everything in the buffer (scrollback included): for selecting text. */
  allText() {
    const buf = this.term.buffer.active;
    const lines = [];
    for (let y = 0; y < buf.length; y++) {
      const line = buf.getLine(y);
      const text = line?.translateToString(true) ?? '';
      if (line?.isWrapped && lines.length) lines[lines.length - 1] += text;
      else lines.push(text);
    }
    return lines.join('\n').replace(/\s+$/, '');
  }

  /** Which screen row a point is on, counted from the bottom (0: the last row). */
  rowFromBottom(clientY) {
    const box = this.term.element.getBoundingClientRect();
    const row = Math.floor(((clientY - box.top) / box.height) * this.term.rows);
    return Math.max(0, Math.min(this.term.rows - 1, this.term.rows - 1 - row));
  }

  // -- touch: one finger scrolls (as wheel events), two fingers zoom, a hold
  // selects text (hooks.onHold) ----

  touch() {
    const host = this.host;
    let start = null;
    let pinch = null;
    let acc = 0;
    let scrolling = false;
    let holdTimer = 0;
    let held = false;
    const dist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    host.addEventListener(
      'touchstart',
      (e) => {
        if (e.touches.length === 2) {
          pinch = { d: dist(e.touches), size: this.term.options.fontSize };
          start = null;
        } else if (e.touches.length === 1) {
          start = { x: e.touches[0].clientX, y: e.touches[0].clientY };
          acc = 0;
          scrolling = false;
          held = false;
          const y = start.y;
          holdTimer = setTimeout(() => {
            held = true;
            this.hooks.onHold?.(y);
          }, 500);
        }
        if (e.touches.length !== 1) clearTimeout(holdTimer);
      },
      { passive: true, capture: true },
    );
    host.addEventListener(
      'touchmove',
      (e) => {
        if (pinch && e.touches.length === 2) {
          e.preventDefault();
          e.stopPropagation();
          this.setFontSize(pinch.size * (dist(e.touches) / pinch.d));
          return;
        }
        if (!start || e.touches.length !== 1) return;
        const t = e.touches[0];
        const dy = start.y - t.clientY;
        if (!scrolling && Math.abs(dy) < 8 && Math.abs(start.x - t.clientX) < 8) return;
        clearTimeout(holdTimer);
        if (!scrolling && Math.abs(dy) < 8) return;
        scrolling = true;
        e.preventDefault();
        e.stopPropagation();
        acc += dy;
        start = { x: t.clientX, y: t.clientY };
        const cell = (this.term.options.fontSize || 14) * 1.1;
        const lines = Math.trunc(acc / cell);
        if (!lines) return;
        acc -= lines * cell;
        const target = document.elementFromPoint(t.clientX, t.clientY) || this.term.element;
        target.dispatchEvent(
          new WheelEvent('wheel', {
            deltaY: lines * cell,
            deltaMode: 0,
            clientX: t.clientX,
            clientY: t.clientY,
            bubbles: true,
            cancelable: true,
          }),
        );
      },
      { passive: false, capture: true },
    );
    const end = (e) => {
      clearTimeout(holdTimer);
      if (held) e.preventDefault?.(); // a hold is not a tap: no keyboard
      held = false;
      if (pinch) this.fitNow();
      pinch = null;
      start = null;
    };
    host.addEventListener('touchend', end, { passive: false, capture: true });
    host.addEventListener('touchcancel', end, { passive: true, capture: true });
  }

  dispose() {
    this.disposed = true;
    this.term?.blur(); // the keyboard goes, and the viewport hears of it (a removed element sends no focusout)
    clearTimeout(this.retryTimer);
    clearTimeout(this.fitTimer);
    document.removeEventListener('visibilitychange', this.onVisible);
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
    this.term?.dispose();
    this.host.replaceChildren();
  }
}
