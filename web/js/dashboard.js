// The dashboard: herdr workspaces and agents, tmux sessions, quick actions.
// Rendering is keyed, so live updates patch cards in place: no flicker, no
// lost press state, no scroll jumps.

import { api, action, here, setMachine } from './api.js';
import { openSheet, closeSheet, item, items, field } from './sheet.js';
import { $, el, glyph, G, toast, tail, ago, storage, store, vibrate } from './util.js';

const STATUS_LABEL = { working: 'working', blocked: 'needs you', done: 'done', idle: 'idle', unknown: '?' };
const QUICK_KEYS = [
  ['esc', 'esc'],
  ['enter', '⏎'],
  ['y', 'y'],
  ['n', 'n'],
  ['1', '1'],
  ['2', '2'],
  ['3', '3'],
  ['up', '↑'],
  ['down', '↓'],
  ['tab', 'tab'],
  ['ctrl+c', '^C'],
];

/** Place `list` into `parent` as keyed children, creating, patching and reordering. */
function reconcile(parent, list, keyOf, make, patch) {
  const old = new Map();
  for (const node of [...parent.children]) {
    if (node.dataset.key === undefined) node.remove(); // placeholders (skeletons)
    else old.set(node.dataset.key, node);
  }
  let prev = null;
  for (const entry of list) {
    const key = keyOf(entry);
    let node = old.get(key);
    if (node) old.delete(key);
    else {
      node = make(entry);
      node.dataset.key = key;
    }
    patch(node, entry);
    const want = prev ? prev.nextElementSibling : parent.firstElementChild;
    if (node !== want) parent.insertBefore(node, want);
    prev = node;
  }
  for (const node of old.values()) node.remove();
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

/** Tap vs long-press on a card. Long-press opens the card's sheet. */
export function pressable(node, { onTap, onHold }) {
  let timer = 0;
  let held = false;
  let origin = null;
  const cancel = () => {
    clearTimeout(timer);
    node.classList.remove('pressed');
  };
  node.addEventListener('pointerdown', (e) => {
    held = false;
    origin = { x: e.clientX, y: e.clientY };
    node.classList.add('pressed');
    timer = setTimeout(() => {
      held = true;
      node.classList.remove('pressed');
      vibrate(15);
      onHold();
    }, 450);
  });
  node.addEventListener('pointermove', (e) => {
    if (origin && Math.hypot(e.clientX - origin.x, e.clientY - origin.y) > 10) cancel();
  });
  for (const ev of ['pointerup', 'pointercancel', 'pointerleave']) node.addEventListener(ev, cancel);
  node.addEventListener('click', () => {
    if (!held) onTap();
    held = false;
  });
  node.addEventListener('contextmenu', (e) => e.preventDefault());
}

const sanitizeName = (s, fallback) => {
  let n = (s || '').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^[^a-z]+/, '').slice(0, 32);
  n = n.replace(/-+$/, '');
  return n || fallback;
};
const tmuxName = (s, fallback) => (s || '').replace(/[^A-Za-z0-9_-]+/g, '-').slice(0, 32) || fallback;

export class Dashboard {
  /**
   * @param {{openTerm:(target:object)=>void}} hooks
   */
  constructor(root, hooks) {
    this.root = root;
    this.hooks = hooks;
    this.config = null; // of the machine shown
    this.state = {}; // of the machine shown
    this.hubConfig = null;
    this.hubState = {}; // this machine's, with the others under .machines
    this.machine = storage('machine', null); // null: this machine
    this.session = storage('herdrSession', null);
    this.previews = new Map(); // key -> {sig, at, text}
    this.inflight = 0;
    this.queue = [];
    this.visible = new Set();
    this.observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) this.visible.add(e.target.dataset.key);
          else this.visible.delete(e.target.dataset.key);
        }
        this.pumpPreviews();
      },
      { root: $('#view-dash') },
    );
    this.tick = setInterval(() => this.pumpPreviews(), 5000);
  }

  setConfig(config) {
    this.hubConfig = config;
    this.config = config;
  }

  // -- machines (this one is the hub for the others) ---------------------

  others() {
    return this.hubState.machines || [];
  }

  remote() {
    return this.machine ? this.others().find((m) => m.name === this.machine) || null : null;
  }

  selectMachine(name) {
    this.machine = name || null;
    store('machine', this.machine);
    this.update(this.hubState);
    this.hooks.onMachine?.();
  }

  /** Terminals and their actions belong to the machine shown when they were opened. */
  open(target) {
    this.hooks.openTerm({ ...target, m: this.machine || undefined });
  }

  // -- state helpers ----------------------------------------------------

  herdrSessions() {
    return this.state.herdr?.sessions || [];
  }

  currentSession() {
    const sessions = this.herdrSessions();
    return sessions.find((s) => s.name === this.session) || sessions.find((s) => s.default) || sessions[0] || null;
  }

  /** Agents waiting, on every machine (the app icon badge). */
  counts() {
    let blocked = 0;
    let done = 0;
    for (const state of [this.hubState, ...this.others().map((m) => m.state || {})]) {
      for (const s of state.herdr?.sessions || []) {
        blocked += s.counts?.blocked || 0;
        done += s.counts?.done || 0;
      }
    }
    return { blocked, done };
  }

  findPane(sessionName, paneId) {
    const s = this.herdrSessions().find((x) => x.name === sessionName);
    for (const ws of s?.workspaces || []) for (const tab of ws.tabs) for (const p of tab.panes) if (p.id === paneId) return { s, ws, tab, p };
    return null;
  }

  // -- render -----------------------------------------------------------

  update(state) {
    this.hubState = state;
    const others = this.others();
    if (this.machine && !others.some((m) => m.name === this.machine)) this.machine = null; // removed meanwhile
    const remote = this.remote();
    this.state = remote ? remote.state || {} : state;
    this.config = remote ? remote.config || this.hubConfig : this.hubConfig;
    setMachine(this.machine);
    const mk = this.machine ? `${this.machine}/` : ''; // keys stay apart across machines
    const blocks = [];
    if (others.length) {
      const list = [
        { key: '.', name: null, label: this.hubConfig?.appName || 'this machine', connected: true, state },
        ...others.map((m) => ({ key: m.name, name: m.name, label: m.name, connected: m.connected, state: m.state })),
      ];
      blocks.push({ key: 'machines', kind: 'machines', list });
    }
    if (remote && !remote.connected) {
      blocks.push({ key: `${mk}offline`, kind: 'empty', text: `${remote.name} is not reachable right now${remote.state ? '; this is what it last reported' : ''}` });
    }
    const herdr = this.state.herdr;
    const tmux = this.state.tmux;
    const sessions = this.herdrSessions();
    const current = this.currentSession();

    if (herdr && sessions.length > 1) blocks.push({ key: `${mk}sessions`, kind: 'sessions', sessions, current });
    if (herdr) {
      if (!current || !current.running) {
        blocks.push({ key: `${mk}herdr-off`, kind: 'empty', text: current ? `herdr session "${current.name}" is not running` : 'herdr is not running' });
      } else if (!current.workspaces.length) {
        blocks.push({ key: `${mk}herdr-none`, kind: 'empty', text: 'No herdr workspaces yet. Tap + to start an agent.' });
      } else {
        for (const ws of current.workspaces) blocks.push({ key: `${mk}ws:${current.name}:${ws.id}`, kind: 'workspace', ws, session: current });
      }
    }
    if (tmux) blocks.push({ key: `${mk}tmux`, kind: 'tmux', tmux });
    if (!remote || remote.connected) blocks.push({ key: `${mk}quick`, kind: 'quick' }); // nothing to open on a machine that is away

    reconcile(
      this.root,
      blocks,
      (b) => b.key,
      (b) => this.makeBlock(b),
      (node, b) => this.patchBlock(node, b),
    );
    this.pumpPreviews();
  }

  makeBlock(b) {
    switch (b.kind) {
      case 'machines':
        return el('div', { class: 'chips machines' });
      case 'sessions':
        return el('div', { class: 'chips' });
      case 'empty':
        return el('div', { class: 'empty' });
      case 'workspace': {
        const menu = el('button', { type: 'button', 'aria-label': 'workspace menu' }, glyph(''));
        const node = el(
          'section',
          { class: 'ws' },
          el('div', { class: 'section-head' }, el('span', { class: 'num' }), el('span', { class: 'label' }), el('span', { class: 'spacer' }), menu),
          el('div', { class: 'cards dash-list' }),
        );
        menu.addEventListener('click', () => this.workspaceSheet(node._ws, node._session));
        return node;
      }
      case 'tmux':
        return el(
          'section',
          { class: 'tmux' },
          el('div', { class: 'section-head' }, el('span', { class: 'label', text: 'tmux' }), el('span', { class: 'spacer' })),
          el('div', { class: 'cards dash-list' }),
        );
      case 'quick':
        return el('section', { class: 'quick-wrap' }, el('div', { class: 'section-head' }, el('span', { class: 'label', text: 'open' })), el('div', { class: 'quick' }));
      default:
        return el('div');
    }
  }

  patchBlock(node, b) {
    if (b.kind === 'empty') setText(node, b.text);
    else if (b.kind === 'machines') this.patchMachines(node, b);
    else if (b.kind === 'sessions') this.patchSessions(node, b);
    else if (b.kind === 'workspace') this.patchWorkspace(node, b);
    else if (b.kind === 'tmux') this.patchTmux(node, b.tmux);
    else if (b.kind === 'quick') this.patchQuick(node);
  }

  patchMachines(node, { list }) {
    const waiting = (state) => (state?.herdr?.sessions || []).reduce((n, s) => n + (s.counts?.blocked || 0) + (s.counts?.done || 0), 0);
    reconcile(
      node,
      list,
      (m) => m.key,
      (m) => {
        const chip = el('button', { class: 'chip machine', type: 'button' }, glyph(G.server), el('span', { class: 'n' }));
        chip.addEventListener('click', () => this.selectMachine(m.name));
        return chip;
      },
      (chip, m) => {
        chip.classList.toggle('on', (m.name || null) === (this.machine || null));
        chip.classList.toggle('live', m.connected);
        chip.title = m.connected ? m.label : `${m.label}: not reachable`;
        const n = waiting(m.state);
        setText(chip.querySelector('.n'), n ? `${m.label} · ${n}` : m.label);
      },
    );
  }

  patchSessions(node, { sessions, current }) {
    reconcile(
      node,
      sessions,
      (s) => s.name,
      (s) => {
        const chip = el('button', { class: 'chip', type: 'button' }, el('span', { class: 'dot' }), el('span', { class: 'n' }));
        chip.addEventListener('click', () => {
          this.session = s.name;
          store('herdrSession', s.name);
          this.update(this.state);
        });
        return chip;
      },
      (chip, s) => {
        chip.classList.toggle('on', s.name === current?.name);
        chip.querySelector('.dot').classList.toggle('run', s.running);
        const n = (s.counts?.blocked || 0) + (s.counts?.done || 0);
        setText(chip.querySelector('.n'), n ? `${s.name} · ${n}` : s.name);
      },
    );
  }

  patchWorkspace(node, { ws, session }) {
    node._ws = ws;
    node._session = session;
    setText(node.querySelector('.num'), String(ws.number ?? ''));
    setText(node.querySelector('.label'), ws.label || ws.id);
    const panes = [];
    for (const tab of ws.tabs) for (const p of tab.panes) panes.push({ p, tab, multiTab: ws.tabs.length > 1 });
    reconcile(
      node.querySelector('.cards'),
      panes,
      ({ p }) => `h:${session.name}:${p.id}`,
      ({ p }) => this.makeCard(),
      (card, { p, tab, multiTab }) => this.patchPaneCard(card, session, ws, tab, p, multiTab),
    );
  }

  makeCard() {
    const card = el(
      'div',
      { class: 'card', role: 'button', tabindex: '0' },
      el('span', { class: 'glyph nf' }),
      el('span', { class: 'name' }),
      el('span', { class: 'pill' }),
      el('span', { class: 'meta' }),
      el('pre', { class: 'preview' }),
      el('i', { class: 'unseen', hidden: true }),
    );
    pressable(card, { onTap: () => card._tap?.(), onHold: () => card._hold?.() });
    this.observer.observe(card);
    return card;
  }

  patchPaneCard(card, session, ws, tab, p, multiTab) {
    const isAgent = !!p.agent;
    card.classList.toggle('plain', !isAgent);
    card.classList.toggle('compact', !isAgent);
    card.classList.toggle('blocked', p.status === 'blocked');
    setText(card.querySelector('.glyph'), isAgent ? G.robot : G.term);
    setText(card.querySelector('.name'), p.name || p.display || p.agent || p.label || 'shell');
    const pill = card.querySelector('.pill');
    pill.hidden = !isAgent;
    if (isAgent) {
      pill.className = `pill ${p.status || 'unknown'}`;
      setText(pill, STATUS_LABEL[p.status] || p.status || '?');
    }
    const meta = [p.title, p.cwd, multiTab ? `tab ${tab.label || tab.number}` : null].filter(Boolean).join(' · ');
    setText(card.querySelector('.meta'), meta || p.id);
    card.querySelector('.unseen').hidden = p.status !== 'done';
    card._preview = {
      key: `${this.machine || ''}/h:${session.name}:${p.id}`,
      sig: `${p.status}:${p.state_change_seq}:${p.completion_seq}`,
      live: p.status === 'working',
      url: here(`/api/preview?k=agent&s=${encodeURIComponent(session.name)}&t=${encodeURIComponent(p.id)}&n=12`),
    };
    if (!isAgent) card._preview = null;
    this.paintPreview(card);
    card._tap = () =>
      isAgent
        ? this.open({ k: 'agent', t: p.id, s: session.name, title: p.name || p.display || p.agent, pane: p.id })
        : this.open({ k: 'herdr', s: session.name, title: `herdr · ${session.name}` });
    card._hold = () => this.paneSheet(session.name, p.id);
  }

  patchTmux(node, tmux) {
    const list = node.querySelector('.cards');
    const sessions = tmux.sessions || [];
    if (!sessions.length) {
      reconcile(list, [{}], () => 'none', () => el('div', { class: 'empty', text: 'No tmux sessions' }), () => {});
      return;
    }
    reconcile(
      list,
      sessions,
      (s) => `t:${s.name}`,
      () => this.makeCard(),
      (card, s) => {
        setText(card.querySelector('.glyph'), G.tmux);
        setText(card.querySelector('.name'), s.name);
        const pill = card.querySelector('.pill');
        pill.hidden = false;
        pill.className = `pill ${s.attached ? 'attached' : 'detached'}`;
        setText(pill, s.attached ? `attached${s.attached > 1 ? ` ×${s.attached}` : ''}` : 'detached');
        setText(card.querySelector('.meta'), `${s.windows} window${s.windows === 1 ? '' : 's'} · ${s.cwd} · ${ago(s.activity)}`);
        card.querySelector('.unseen').hidden = !s.unseen;
        card._preview = {
          key: `${this.machine || ''}/t:${s.name}`,
          sig: String(s.activity),
          live: false,
          url: here(`/api/preview?k=tmux&t=${encodeURIComponent(s.name)}&n=12`),
        };
        this.paintPreview(card);
        card._tap = () => this.open({ k: 'tmux', t: s.name, title: s.name });
        card._hold = () => this.tmuxSheet(s);
      },
    );
  }

  patchQuick(node) {
    const grid = node.querySelector('.quick');
    if (grid.childElementCount) return;
    const p = this.config?.providers || {};
    const buttons = [];
    if (p.shell) buttons.push(this.quickButton(G.term, 'shell', () => this.open({ k: 'shell', title: 'shell' })));
    const def = this.state.tmux?.default;
    if (p.tmux && def) buttons.push(this.quickButton(G.tmux, def, () => this.open({ k: 'tmux', t: def, title: def })));
    if (p.herdr)
      buttons.push(
        this.quickButton(G.robot, 'full herdr', () => {
          const s = this.currentSession();
          this.open({ k: 'herdr', s: s?.name, title: `herdr · ${s?.name || 'default'}` });
        }),
      );
    grid.replaceChildren(...buttons);
  }

  quickButton(icon, label, onClick) {
    return el('button', { class: 'btn', type: 'button', onclick: onClick }, glyph(icon), el('span', { text: label }));
  }

  // -- previews ---------------------------------------------------------

  paintPreview(card) {
    const pre = card.querySelector('.preview');
    const info = card._preview;
    const cached = info && this.previews.get(info.key);
    setText(pre, cached ? cached.text : '');
  }

  pumpPreviews() {
    if (document.visibilityState !== 'visible' || !$('#app').matches('[data-view="dash"]')) return;
    const now = Date.now();
    for (const card of this.root.querySelectorAll('.card')) {
      const info = card._preview;
      if (!info || !this.visible.has(card.dataset.key)) continue;
      const cached = this.previews.get(info.key);
      const stale = !cached || cached.sig !== info.sig || (info.live && now - cached.at > 5000) || now - cached.at > 60000;
      if (stale && !this.queue.some((q) => q.key === info.key) && !(cached && cached.loading)) this.queue.push(info);
    }
    while (this.inflight < 2 && this.queue.length) this.fetchPreview(this.queue.shift());
  }

  async fetchPreview(info) {
    this.inflight++;
    const entry = this.previews.get(info.key) || { text: '' };
    entry.loading = true;
    this.previews.set(info.key, entry);
    try {
      const res = await api(info.url);
      this.previews.set(info.key, { sig: info.sig, at: Date.now(), text: tail(res.text, 2) });
    } catch {
      this.previews.set(info.key, { sig: info.sig, at: Date.now(), text: entry.text || '' });
    } finally {
      this.inflight--;
    }
    for (const card of this.root.querySelectorAll('.card')) if (card._preview?.key === info.key) this.paintPreview(card);
    this.pumpPreviews();
  }

  // -- sheets -----------------------------------------------------------

  async paneSheet(sessionName, paneId) {
    const found = this.findPane(sessionName, paneId);
    if (!found) return;
    const { p } = found;
    const isAgent = !!p.agent;
    const run = (op, params, ok) =>
      action(op, { session: sessionName, ...params })
        .then(() => ok && toast(ok))
        .catch((e) => toast(e.message, { error: true }));
    const pre = el('pre', { class: 'preview', text: '…' });
    const body = [pre];
    if (isAgent) {
      body.push(
        el(
          'div',
          { class: 'keyrow' },
          ...QUICK_KEYS.map(([key, label]) =>
            el('button', { class: 'key', type: 'button', onclick: () => run('keys', { target: p.id, keys: [key] }) }, label),
          ),
        ),
      );
    }
    body.push(
      items(
        isAgent
          ? item({ icon: G.term, label: 'Open terminal', onSelect: () => this.open({ k: 'agent', t: p.id, s: sessionName, title: p.name || p.display || p.agent, pane: p.id }) })
          : item({ icon: G.robot, label: 'Open in full herdr', onSelect: () => this.open({ k: 'herdr', s: sessionName, title: `herdr · ${sessionName}` }) }),
        isAgent && item({ icon: G.eye, label: 'Mark seen (focuses it in herdr)', onSelect: () => run('focus', { target: p.id }) }),
        !isAgent && item({ icon: G.eye, label: 'Focus in herdr', onSelect: () => run('focus_pane', { target: p.id }) }),
        isAgent && item({ icon: G.send, label: 'Prompt…', onSelect: () => this.promptSheet(sessionName, p) }),
        isAgent && item({ icon: G.pencil, label: 'Rename…', onSelect: () => this.renameAgentSheet(sessionName, p) }),
        item({ icon: G.trash, label: 'Close pane', danger: true, hold: true, onSelect: () => run('close_pane', { target: p.id }, 'Pane closed') }),
      ),
    );
    openSheet({ title: p.name || p.display || p.agent || 'shell pane', sub: [p.title, p.cwd, p.id].filter(Boolean).join(' · '), body });
    try {
      const res = await api(here(`/api/preview?k=agent&s=${encodeURIComponent(sessionName)}&t=${encodeURIComponent(p.id)}&n=40`));
      pre.textContent = tail(res.text, 40) || '(empty)';
      pre.scrollTop = pre.scrollHeight;
    } catch (e) {
      pre.textContent = e.message;
    }
  }

  workspaceSheet(ws, session) {
    openSheet({
      title: ws.label || ws.id,
      sub: `workspace ${ws.number} · ${session.name}`,
      body: [
        items(
          item({ icon: G.pencil, label: 'Rename…', onSelect: () => this.renameSheet('Rename workspace', ws.label, (name) => action('rename_workspace', { session: session.name, id: ws.id, name })) }),
          ws.tabs.length > 1 &&
            ws.tabs.map((tab) =>
              item({ icon: G.pencil, label: `Rename tab ${tab.number}…`, onSelect: () => this.renameSheet('Rename tab', tab.label, (name) => action('rename_tab', { session: session.name, id: tab.id, name })) }),
            ),
          item({
            icon: G.trash,
            label: 'Close workspace',
            danger: true,
            hold: true,
            onSelect: () =>
              action('close_workspace', { session: session.name, id: ws.id })
                .then(() => toast('Workspace closed'))
                .catch((e) => toast(e.message, { error: true })),
          }),
        ),
      ],
    });
  }

  tmuxSheet(s) {
    openSheet({
      title: s.name,
      sub: `tmux · ${s.windows} windows · ${s.cwd}`,
      body: [
        items(
          item({ icon: G.term, label: 'Attach', onSelect: () => this.open({ k: 'tmux', t: s.name, title: s.name }) }),
          item({ icon: G.pencil, label: 'Rename…', onSelect: () => this.renameSheet('Rename session', s.name, (name) => action('tmux_rename', { target: s.name, name })) }),
          item({
            icon: G.trash,
            label: 'Kill session',
            danger: true,
            hold: true,
            onSelect: () =>
              action('tmux_kill', { target: s.name })
                .then(() => toast('Session killed'))
                .catch((e) => toast(e.message, { error: true })),
          }),
        ),
      ],
    });
  }

  renameSheet(title, current, save) {
    const input = el('input', { type: 'text', value: current || '', maxlength: '64', autocapitalize: 'off', autocomplete: 'off', spellcheck: 'false' });
    const submit = () =>
      save(input.value.trim())
        .then(() => closeSheet())
        .catch((e) => toast(e.message, { error: true }));
    input.addEventListener('keydown', (e) => e.key === 'Enter' && submit());
    openSheet({ title, body: [field('name', input), el('button', { class: 'btn primary wide', type: 'button', onclick: submit }, 'Save')] });
    setTimeout(() => input.focus(), 60);
  }

  renameAgentSheet(sessionName, p) {
    this.renameSheet('Rename agent', p.name || '', (name) => action('rename_agent', { session: sessionName, target: p.id, name: name || null }));
  }

  promptSheet(sessionName, p) {
    const text = el('textarea', { placeholder: 'Prompt for the agent…', maxlength: '4000', autocapitalize: 'sentences' });
    const send = () =>
      action('prompt', { session: sessionName, target: p.id, text: text.value })
        .then(() => {
          closeSheet();
          toast('Sent');
        })
        .catch((e) => toast(e.message, { error: true }));
    const row = el('div', { class: 'row' });
    if (this.hooks.dictateInto) {
      const mic = el('button', { class: 'btn', type: 'button' }, glyph(G.mic), el('span', { class: 'time', text: '' }));
      // Partial text streams into the box after whatever was typed before.
      let base = '';
      let finished = false;
      const join = (t) => (base && t ? `${base} ${t}` : base || t);
      const sink = {
        begin() {
          base = text.value.trim();
          finished = false;
        },
        partial: (t) => (text.value = join(t)),
        done(t, { send: andSend }) {
          text.value = join(t);
          finished = true;
          if (andSend) send();
        },
        end: () => !finished && (text.value = base), // failed or heard nothing: undo the partials
      };
      mic.addEventListener('click', () => this.hooks.dictateInto(mic, sink));
      row.append(mic);
    }
    if (this.hooks.attachInto) {
      const add = (args) => (text.value = text.value.trim() ? `${text.value.trim()} ${args}` : args);
      row.append(el('button', { class: 'btn', type: 'button', 'aria-label': 'attach an image', onclick: () => this.hooks.attachInto(add) }, glyph(G.image)));
    }
    row.append(el('button', { class: 'btn primary', type: 'button', onclick: send }, glyph(G.send), 'Send'));
    openSheet({ title: `Prompt ${p.name || p.display || p.agent}`, sub: p.title || p.cwd || '', body: [text, row] });
    setTimeout(() => text.focus(), 60);
  }

  async newSheet() {
    const p = this.config.providers;
    const tabs = [];
    const kinds = this.state.herdr?.agentKinds || [];
    if (p.herdr && kinds.length) tabs.push(['agent', 'Agent']);
    if (p.tmux) tabs.push(['tmux', 'tmux']);
    if (p.shell) tabs.push(['shell', 'Shell']);
    if (!tabs.length) return;
    let mode = storage('newMode', tabs[0][0]);
    if (!tabs.some(([k]) => k === mode)) mode = tabs[0][0];

    const project = el('select');
    const kind = el('select', {}, ...kinds.map((k) => el('option', { value: k, text: k })));
    const name = el('input', { type: 'text', maxlength: '32', autocapitalize: 'off', autocomplete: 'off', spellcheck: 'false' });
    const sessionSel = el('select', {}, ...this.herdrSessions().filter((s) => s.running).map((s) => el('option', { value: s.name, text: s.name })));
    sessionSel.value = this.currentSession()?.name || '';
    let touched = false;
    name.addEventListener('input', () => (touched = true));
    const projName = () => {
      const v = project.value || '';
      return v.split('/').filter(Boolean).pop() || 'home';
    };
    const suggest = () => {
      if (touched) return;
      name.value = mode === 'agent' ? sanitizeName(`${projName()}-${kind.value}`, kind.value || 'agent') : tmuxName(projName(), 'main');
    };
    project.addEventListener('change', suggest);
    kind.addEventListener('change', suggest);

    const form = el('div');
    const seg = el('div', { class: 'seg' });
    const go = el('button', { class: 'btn primary wide', type: 'button' });
    const paint = () => {
      store('newMode', mode);
      for (const b of seg.children) b.classList.toggle('on', b.dataset.mode === mode);
      if (mode === 'shell') {
        form.replaceChildren(el('p', { class: 'hint', text: `A fresh login shell. It ends when you close the terminal.` }));
        go.replaceChildren(glyph(G.term), 'Open shell');
      } else if (mode === 'tmux') {
        form.replaceChildren(field('directory', project), field('session name', name));
        go.replaceChildren(glyph(G.tmux), 'Create session');
      } else {
        const rows = [field('agent', kind), field('project', project), field('name', name)];
        if (sessionSel.options.length > 1) rows.unshift(field('herdr session', sessionSel));
        form.replaceChildren(...rows);
        go.replaceChildren(glyph(G.robot), 'Start agent');
      }
      touched = false;
      suggest();
    };
    for (const [k, label] of tabs) {
      const b = el('button', { type: 'button', dataset: { mode: k }, text: label });
      b.addEventListener('click', () => {
        mode = k;
        paint();
      });
      seg.append(b);
    }
    go.addEventListener('click', async () => {
      go.disabled = true;
      try {
        if (mode === 'shell') {
          closeSheet();
          this.open({ k: 'shell', title: 'shell' });
        } else if (mode === 'tmux') {
          await action('tmux_new', { name: name.value.trim(), cwd: project.value });
          closeSheet();
          this.open({ k: 'tmux', t: name.value.trim(), title: name.value.trim() });
        } else {
          go.replaceChildren(glyph(G.hourglass), 'Starting…');
          const res = await action('new_agent', { session: sessionSel.value, kind: kind.value, cwd: project.value, name: name.value.trim() });
          closeSheet();
          if (res.warning) toast(res.warning);
          this.open({ k: 'agent', t: res.pane, s: sessionSel.value, title: name.value.trim(), pane: res.pane });
        }
      } catch (e) {
        toast(e.message, { error: true });
        paint();
      } finally {
        go.disabled = false;
      }
    });
    openSheet({ title: 'New', body: [tabs.length > 1 ? seg : null, form, go] });
    paint();

    try {
      const { roots } = await api(here('/api/projects'));
      const home = this.config.home || '';
      const pretty = (path) => (home && path.startsWith(home) ? `~${path.slice(home.length)}` : path);
      const groups = roots.map((r) =>
        el(
          'optgroup',
          { label: pretty(r.path) },
          el('option', { value: r.path, text: pretty(r.path) }),
          ...r.dirs.map((d) => el('option', { value: `${r.path}/${d}`, text: d })),
        ),
      );
      project.replaceChildren(...groups);
      const last = storage('lastProject', null);
      if (last && [...project.options].some((o) => o.value === last)) project.value = last;
      project.addEventListener('change', () => store('lastProject', project.value));
      suggest();
    } catch (e) {
      toast(e.message, { error: true });
    }
  }

  inboxSheet() {
    const alerts = this.hubState.alerts?.items || []; // every machine's (theirs are forwarded here)
    const list = alerts.length
      ? alerts.map((a) => {
          const icon = a.kind === 'blocked' ? G.bell : a.kind === 'done' ? G.check : G.bell;
          const row = el(
            'button',
            { class: `alert ${a.kind}`, type: 'button' },
            glyph(icon),
            el('span', { class: 't', text: a.machine ? `${a.machine} · ${a.title || a.message}` : a.title || a.message }),
            el('span', { class: 'when', text: ago(a.time) }),
            el('span', { class: 'm', text: a.message }),
          );
          row.addEventListener('click', () => {
            if ((a.machine || null) !== (this.machine || null)) this.selectMachine(a.machine || null);
            const match = this.matchAlert(a);
            closeSheet();
            if (match) this.open({ k: 'agent', t: match.p.id, s: match.s.name, title: match.p.name || match.p.display || match.p.agent, pane: match.p.id });
            else toast('No matching agent pane right now');
          });
          return row;
        })
      : [el('div', { class: 'empty', text: 'No alerts in the last hour' })];
    const al = this.hubState.alerts;
    openSheet({ title: 'Inbox', sub: !al?.ntfy ? 'from your agents' : al.connected ? 'from your agents and ntfy' : 'ntfy not connected', body: [el('div', { class: 'items' }, ...list)] });
  }

  matchAlert(a) {
    if (!a.project) return null;
    for (const s of this.herdrSessions())
      for (const ws of s.workspaces || [])
        for (const tab of ws.tabs)
          for (const p of tab.panes)
            if (p.agent && p.cwd === a.project && (!a.agent || (p.agent || '').toLowerCase().includes(a.agent.toLowerCase().split(' ')[0])))
              return { s, ws, tab, p };
    return null;
  }
}
