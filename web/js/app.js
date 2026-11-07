// lariat: boot, views (login / dashboard / terminal), live events, menus.

import { api, at, Events } from './api.js';
import { Login, Enroll, logout, webauthnSupported } from './auth.js';
import { initViewport, onViewport, settleViewport } from './viewport.js';
import { Dashboard } from './dashboard.js';
import { TermView } from './term.js';
import { KeyBar } from './keys.js';
import { Recorder, primeAudio } from './dictate.js';
import { pickImages, readClipboard, uploadImages, asArgs } from './upload.js';
import { pushSupport, currentSubscription, enablePush, disablePush, testPush, pushKinds, updatePushKinds, trackPresence } from './push.js';
import { openSheet, closeSheet, item, items, tiles, tile, section, toggle, choice, stepper, note } from './sheet.js';
import { $, el, G, toast, storage, store, vibrate, spokenKey } from './util.js';
import { updatesSheet, refreshUpdates, pendingUpdates } from './updates.js';
import { machinesSheet, machinesChanged } from './machines.js';
import { openSelect, closeSelect } from './select.js';

const app = $('#app');
const login = new Login();
const enroll = new Enroll();
const recorder = new Recorder();
let info = { enroll: false };
let config = null;
let dash = null;
let events = null;
let term = null;
let keybar = null;
let target = null;
let offlineTimer = 0;

// -- views ----------------------------------------------------------------

function setView(view) {
  app.dataset.view = view;
  settleViewport();
  const p = (dash?.config || config)?.providers || {}; // the machine the dashboard shows
  const onDash = view === 'dash';
  $('#act-herdr').hidden = !(onDash && p.herdr);
  $('#act-tmux').hidden = !(onDash && p.tmux && dash?.state.tmux?.default);
  $('#act-inbox').hidden = !(onDash && config?.providers?.alerts); // the inbox is every machine's
  $('#act-new').hidden = !onDash;
  $('#act-menu').hidden = !(onDash || view === 'term');
  $('#term-status').hidden = view !== 'term';
  if (view !== 'term') $('#title').textContent = dash?.machine || config?.appName || document.title;
  if (onDash) dash?.pumpPreviews();
}

function setConn(state) {
  $('#conn').className = `conn ${state}`;
}

// -- login and enrollment -------------------------------------------------

function loginMessage(text, error = false) {
  const m = $('#login-msg');
  m.textContent = text || ' ';
  m.classList.toggle('error', error);
}

function explain(e) {
  if (e?.name === 'NotAllowedError') return 'Cancelled';
  if (e?.name === 'InvalidStateError') return 'This passkey is already enrolled';
  if (e?.status === 0) return 'Server unreachable';
  return e?.message || String(e);
}

// The QR code from `lariat-enroll` opens /enroll#t=<token>. The fragment never
// reaches the server; take the token and drop it from the address bar.
const link = new URLSearchParams(location.hash.slice(1));
const linkToken = link.get('t') || '';
// A notification opens /#go=agent:<session>:<pane>: that agent, once logged in.
let linkGo = link.get('go') || '';
if (location.hash) history.replaceState(null, '', location.pathname + location.search);

const deviceName = () => (navigator.userAgent.match(/iPhone|iPad|Android|Macintosh|Windows|Linux/) || ['passkey'])[0];

function useLinkToken() {
  $('#enroll-token').value = linkToken;
  $('#enroll-token').hidden = true;
  if (!$('#enroll-label').value) $('#enroll-label').value = deviceName();
  $('#enroll-hint').textContent = 'Enrollment code loaded from the QR code.';
  $('#unlock').classList.remove('primary');
  $('#enroll-go').classList.add('primary');
  enroll.prefetch(linkToken).catch((e) => loginMessage(explain(e), true));
}

function showLogin(message = '') {
  closeSheet();
  setView('login');
  loginMessage(message);
  $('#enroll').hidden = !info.enroll;
  if (linkToken && !info.enroll) loginMessage('Enrollment is closed; that code was used already', true);
  if (!webauthnSupported()) {
    loginMessage('This browser has no passkey support', true);
    return;
  }
  login.prefetch().catch((e) => loginMessage(explain(e), true));
  if (linkToken && info.enroll) useLinkToken();
}

async function unlock() {
  loginMessage('');
  try {
    await login.run();
    await enterApp();
  } catch (e) {
    loginMessage(explain(e), true);
    login.prefetch().catch(() => {});
  }
}

async function doEnroll() {
  const token = $('#enroll-token').value.trim();
  const label = $('#enroll-label').value.trim() || 'passkey';
  if (token.length < 16) {
    loginMessage('Paste the enrollment token first', true);
    return;
  }
  try {
    await enroll.run(token, label);
    $('#enroll-token').value = '';
    info.enroll = false;
    // Installing from here must not start the app at /enroll, which 404s once enrollment is off.
    if (location.pathname !== '/') history.replaceState(null, '', '/');
    const installed = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
    toast(installed ? 'Passkey created' : 'Passkey created. Install the app: Share → Add to Home Screen', { ms: 8000 });
    await enterApp();
  } catch (e) {
    loginMessage(explain(e), true);
  }
}

// -- the app ----------------------------------------------------------------

async function enterApp() {
  let data;
  try {
    data = await api('/api/state');
  } catch (e) {
    if (e.status === 401) return showLogin();
    return showOffline();
  }
  clearTimeout(offlineTimer);
  login.stop();
  config = data.config;
  document.title = config.appName;
  if (config.dictation && Recorder.supported() && !keybar.hooks.dictation) {
    keybar.hooks.dictation = true;
    keybar.render();
    dash.hooks.dictateInto = toggleDictation;
    // iOS starts audio only from a tap's end: get it ready on every tap.
    for (const ev of ['pointerup', 'touchend', 'keydown']) document.addEventListener(ev, primeAudio, { capture: true, passive: true });
  }
  dash.setConfig(config);
  dash.update(data.state);
  events.start();
  if (config.push && !presenceOn) {
    presenceOn = true;
    trackPresence();
  }
  if (linkGo) {
    const go = linkGo;
    linkGo = '';
    if (openGo(go)) return;
  }
  if (term) {
    setView('term');
    closeSheet();
    term.reconnect();
  } else {
    setView('dash');
  }
}

let presenceOn = false;

/** Open what a notification points at; false if it is gone. */
function openGo(go) {
  if (go.startsWith('m:')) {
    // Another machine's alert: m:<machine>:agent:<session>:<pane>
    const [, machine, ...more] = go.split(':');
    if (!dash.others().some((m) => m.name === machine)) {
      toast(`${machine} is no longer linked here`);
      return false;
    }
    if (dash.machine !== machine) dash.selectMachine(machine);
    go = more.join(':');
  }
  const [kind, session, ...rest] = go.split(':');
  const pane = rest.join(':'); // pane ids contain colons
  if (kind !== 'agent' || !session || !pane) return false;
  const found = dash.findPane(session, pane);
  if (!found) {
    toast('That agent is gone');
    return false;
  }
  const p = found.p;
  openTerm({ k: 'agent', t: p.id, s: session, title: p.name || p.display || p.agent, pane: p.id, m: dash.machine || undefined });
  return true;
}

function showOffline() {
  setView('login');
  $('#enroll').hidden = true;
  loginMessage("Can't reach the server. Retrying…", true);
  clearTimeout(offlineTimer);
  offlineTimer = setTimeout(() => boot(false), 5000);
}

function onExpired() {
  events.stop();
  if (term) {
    // Keep the frozen terminal on screen; unlock reconnects to the same target.
    openSheet({
      title: 'Session expired',
      sub: 'Unlock to continue where you were.',
      body: [
        el('button', {
          class: 'btn primary wide',
          type: 'button',
          onclick: () =>
            login
              .run()
              .then(enterApp)
              .catch((e) => toast(explain(e), { error: true })),
          text: 'Unlock',
        }),
      ],
    });
    login.prefetch().catch(() => {});
  } else {
    showLogin('Session expired');
  }
}

function updateBadges(state) {
  const { blocked, done } = dash.counts();
  try {
    if (navigator.setAppBadge) {
      const n = blocked + done;
      if (n) navigator.setAppBadge(n).catch(() => {});
      else navigator.clearAppBadge?.().catch(() => {});
    }
  } catch {
    /* badges are optional */
  }
  const latest = state.alerts?.items?.[0]?.time || 0;
  $('#inbox-badge').hidden = !(latest > storage('inboxSeen', 0));
  $('#act-tmux').hidden = !(app.dataset.view === 'dash' && config?.providers.tmux && state.tmux?.default);
}

// -- terminal ---------------------------------------------------------------

function prefixFor(t) {
  const s = dash.state;
  if (t.k === 'agent' || t.k === 'herdr') return s.herdr?.prefix;
  if (t.k === 'tmux') return s.tmux?.prefix;
  return null;
}

// iOS drops a home-screen app's microphone grant on every same-page navigation
// (pushState, back, hash changes), which made dictation ask again after each
// terminal. So there the terminal is no history entry; the back button closes
// it (iOS web apps have no back gesture to keep). Elsewhere, Back closes it.
const IOS = /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const TERM_HISTORY = !IOS;

/** Select text in the terminal (a hold on it, or its menu): `fromBottom` is the row held. */
function selectText(fromBottom = 0) {
  if (!term) return;
  const t = target;
  term.term.blur(); // the keyboard away: room for the text
  let load = null;
  if (t.k === 'agent' || t.k === 'tmux') {
    const q = t.k === 'agent' ? `k=agent&s=${encodeURIComponent(t.s || '')}&t=${encodeURIComponent(t.t)}` : `k=tmux&t=${encodeURIComponent(t.t)}`;
    load = api(at(t.m, `/api/preview?${q}&n=2000`)).then((r) => r.text); // the pane's history, unwrapped
  }
  openSelect({ text: term.allText(), load, fromBottom });
}

async function openTerm(t) {
  closeSheet();
  if (term) term.dispose();
  target = t;
  $('#title').textContent = t.m ? `${t.m} · ${t.title || t.k}` : t.title || t.k;
  $('#term-overlay').hidden = true;
  setView('term');
  if (TERM_HISTORY && history.state?.term !== true) history.pushState({ term: true }, '');
  keybar.setPrefix(prefixFor(t));
  term = new TermView($('#term-host'), t, {
    onStatus: termStatus,
    onExpired,
    transform: (d) => keybar.transform(d),
    onHold: (y) => selectText(term.rowFromBottom(y)),
  });
  await term.open();
}

function closeTerm(fromHistory = false) {
  closeSelect();
  if (dict.state !== 'idle') {
    recorder.cancel();
    endDictation();
  }
  if (term) term.dispose();
  term = null;
  target = null;
  $('#term-overlay').hidden = true;
  setView('dash');
  if (TERM_HISTORY && !fromHistory && history.state?.term === true) history.back();
}

function termStatus(status, msg) {
  const pill = $('#term-status');
  pill.className = `pill ${status}`;
  pill.textContent = status === 'live' ? 'live' : status === 'connecting' ? '…' : status;
  const overlay = $('#term-overlay');
  const show = status === 'exited' || (status === 'offline' && msg !== 'Session expired');
  overlay.hidden = !show;
  if (show) {
    $('#term-overlay-msg').textContent = msg || status;
    $('#term-reconnect').textContent = target?.k === 'shell' ? 'New shell' : 'Reconnect';
  }
}

function termMenu() {
  if (!term) return;
  const t = target;
  const copy = (text, what) =>
    navigator.clipboard
      .writeText(text)
      .then(() => toast(`${what} copied`))
      .catch(() => toast('Clipboard not available', { error: true }));
  const found = t.k === 'agent' ? dash.findPane(t.s, t.t) : null;
  const size = () => term.term.options.fontSize;
  openSheet({
    title: t.title || t.k,
    sub: { agent: `herdr agent · ${t.t}`, herdr: `herdr · ${t.s || 'default'}`, tmux: 'tmux session', shell: 'login shell' }[t.k],
    body: [
      tiles(
        tile({ icon: G.select, label: 'Select text', onSelect: () => selectText(0) }),
        tile({ icon: G.copy, label: 'Copy screen', onSelect: () => copy(term.screenText(), 'Screen') }),
        found && tile({ icon: G.send, label: 'Prompt', onSelect: () => dash.promptSheet(t.s, found.p) }),
        found &&
          tile({
            icon: G.eye,
            label: 'Mark seen',
            onSelect: () => api(at(t.m, '/api/action'), { method: 'POST', body: { op: 'focus', session: t.s, target: t.t } }).catch((e) => toast(e.message, { error: true })),
          }),
        tile({ icon: G.refresh, label: t.k === 'shell' ? 'New shell' : 'Reconnect', onSelect: () => term.reconnect() }),
      ),
      config?.dictation &&
        section(
          'Dictation',
          choice({
            icon: G.timer,
            label: 'Stop when I pause',
            options: PAUSES,
            value: storage('dictatePause', '1'),
            onChange: (v) => store('dictatePause', v),
          }),
          toggle({
            icon: G.enter,
            label: 'Then press Enter (send)',
            value: storage('dictateSend', '1') === '1',
            onChange: (on) => store('dictateSend', on ? '1' : '0'),
          }),
        ),
      section(
        'Display',
        stepper({
          icon: G.textSize,
          label: 'Text size',
          value: size(),
          onStep: (d) => {
            term.setFontSize(size() + d);
            return size();
          },
        }),
        toggle({
          icon: G.monitor,
          label: 'WebGL renderer',
          value: storage('renderer', 'dom') === 'webgl',
          onChange: (on) => {
            store('renderer', on ? 'webgl' : 'dom');
            closeSheet();
            openTerm(t);
          },
        }),
      ),
      items(item({ icon: G.close, label: 'Close terminal', danger: true, onSelect: () => closeTerm() })),
    ],
  });
}

/** This machine and the ones it is the hub for, for the updates sheet. */
function allMachines() {
  const here = { name: null, label: config?.appName || 'this machine', connected: true, state: dash.hubState };
  return [here, ...dash.others().map((m) => ({ name: m.name, label: m.name, connected: m.connected, state: m.state }))];
}

async function dashMenu() {
  const support = config?.push ? pushSupport() : null;
  const sub = support?.ok ? await currentSubscription().catch(() => null) : null;
  const on = !!sub && Notification.permission === 'granted';
  const kinds = pushKinds();
  const setKind = (kind, want) => {
    const next = want ? [...new Set([...pushKinds(), kind])] : pushKinds().filter((k) => k !== kind);
    return updatePushKinds(next).catch((e) => {
      toast(explain(e), { error: true });
      throw e;
    });
  };
  openSheet({
    title: config?.appName || 'lariat',
    body: [
      support &&
        section(
          'Notifications',
          support.ok
            ? toggle({
                icon: G.bell,
                label: 'Push notifications',
                value: on,
                onChange: (want) =>
                  (want ? enablePush(config.push.key) : disablePush())
                    .then(() => toast(want ? 'Notifications on' : 'Notifications off'))
                    .catch((e) => {
                      toast(explain(e), { error: true });
                      throw e;
                    }),
              })
            : note(support.why),
          support.ok && toggle({ icon: G.warn, label: 'When an agent needs you', value: kinds.includes('blocked'), onChange: (v) => setKind('blocked', v) }),
          support.ok && toggle({ icon: G.check, label: 'When an agent finishes', value: kinds.includes('done'), onChange: (v) => setKind('done', v) }),
          support.ok &&
            item({
              icon: G.send,
              label: 'Send a test',
              close: false,
              onSelect: () =>
                testPush()
                  .then(() => toast('Sent. Lock the phone to see it.'))
                  .catch((e) => toast(explain(e), { error: true })),
            }),
        ),
      items(
        item({
          icon: G.update,
          label: pendingUpdates(allMachines()) ? `Updates · ${pendingUpdates(allMachines())} available` : 'Updates',
          onSelect: () => updatesSheet(allMachines),
        }),
        item({ icon: G.refresh, label: 'Reload app', onSelect: () => location.reload() }),
        item({
          icon: G.signout,
          label: 'Lock',
          onSelect: () => {
            events.stop();
            logout()
              .catch(() => {})
              .then(() => showLogin('Locked'));
          },
        }),
      ),
    ],
  });
}

// -- dictation ---------------------------------------------------------------

// Tap the mic to start and again to stop, or hold it while talking and let go.
// Partial text shows while you speak. With auto-stop on, a pause after speech
// ends the recording; "send" also presses Enter. Both are per-device settings.

const HOLD_MS = 350;
const dict = { state: 'idle', btn: null, sink: null, pressedAt: 0, stopOnRelease: false }; // idle | starting | recording | finishing

const PAUSES = [['0', 'Off'], ['0.6', '0.6s'], ['1', '1s'], ['1.5', '1.5s'], ['2.5', '2.5s']];
const dictSettings = () => ({
  pauseMs: Number(storage('dictatePause', '1')) * 1000 || 0,
  send: storage('dictateSend', '1') === '1',
});

function micDown(btn, sink) {
  if (dict.state === 'recording') dict.stopOnRelease = true;
  else if (dict.state === 'starting') cancelDictation(); // a tap while starting gives up
  if (dict.state !== 'idle') return;
  dict.pressedAt = Date.now();
  dict.stopOnRelease = false;
  startDictation(btn, sink);
}

function micUp() {
  // Released while still starting (permission prompt, say): keep recording, tap to stop.
  if (dict.state === 'recording' && (dict.stopOnRelease || Date.now() - dict.pressedAt >= HOLD_MS)) finishDictation();
}

function toggleDictation(btn, sink) {
  if (dict.state === 'recording') finishDictation();
  else if (dict.state === 'starting') cancelDictation();
  else if (dict.state === 'idle') startDictation(btn, sink);
}

function setMicTime(btn, s) {
  const time = btn?.querySelector('.time');
  if (time) time.textContent = s == null ? '' : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

async function startDictation(btn, sink) {
  const settings = dictSettings();
  Object.assign(dict, { state: 'starting', btn, sink, settings });
  btn.classList.add('rec');
  sink.begin?.(settings);
  try {
    await recorder.start({
      pauseMs: settings.pauseMs,
      onTick: (s) => setMicTime(btn, s),
      onLimit: () => dict.state === 'recording' && finishDictation(),
      onPartial: (text) => dict.sink === sink && sink.partial?.(text),
      onPause: () => dict.state === 'recording' && finishDictation(),
      onPauseProgress: (f) => sink.pause?.(f),
      onLevel: (v) => {
        const level = Math.min(1, v * 2.5).toFixed(2);
        btn.style.setProperty('--level', level);
        sink.level?.(level);
      },
      onError: (e) => {
        if (dict.state !== 'recording') return;
        cancelDictation();
        toast(explain(e), { error: true });
      },
    });
    if (dict.state !== 'starting' || dict.btn !== btn) return; // cancelled meanwhile
    dict.state = 'recording';
    vibrate(10);
  } catch (e) {
    if (dict.btn === btn) endDictation();
    if (e?.message !== 'Cancelled') toast(e?.name === 'NotAllowedError' ? 'Microphone permission denied' : explain(e), { error: true });
  }
}

async function finishDictation() {
  const { btn, sink, settings } = dict;
  dict.state = 'finishing';
  btn.classList.remove('rec');
  btn.classList.add('busy');
  sink.busy?.();
  try {
    const text = await recorder.stop();
    if (text) sink.done(text, settings);
    else toast('Heard nothing');
  } catch (e) {
    toast(explain(e), { error: true });
  } finally {
    if (dict.btn === btn) endDictation();
  }
}

function cancelDictation() {
  recorder.cancel();
  endDictation();
}

function endDictation() {
  const { btn, sink } = dict;
  btn?.classList.remove('rec', 'busy');
  btn?.style.removeProperty('--level');
  setMicTime(btn, null);
  sink?.end?.();
  Object.assign(dict, { state: 'idle', btn: null, sink: null, settings: null });
}

/** Dictating into the terminal: a live transcript over its bottom, then a paste. */
const termSink = {
  begin({ pauseMs, send }) {
    const box = $('#dictation');
    box.classList.remove('busy');
    box.style.setProperty('--pause', 0);
    $('#dictation-text').textContent = '';
    $('#dictation-hint').textContent = pauseMs
      ? `Pause ${pauseMs / 1000}s to ${send ? 'send' : 'insert'}`
      : `Tap the mic to ${send ? 'send' : 'insert'}`;
    box.hidden = false;
  },
  partial: (text) => ($('#dictation-text').textContent = text),
  level: (v) => $('#dictation').style.setProperty('--level', v),
  pause: (f) => $('#dictation').style.setProperty('--pause', f.toFixed(2)),
  busy: () => $('#dictation').classList.add('busy'),
  done(text, { send }) {
    const key = spokenKey(text);
    if (key) return term?.sendText(key); // a menu choice: the key itself, and never an Enter after it
    term?.paste(send ? text : `${text} `);
    if (send) term?.sendText('\r');
  },
  end: () => ($('#dictation').hidden = true),
};

// -- images ------------------------------------------------------------------------

/** Upload images to machine `m` (where the session is) and hand their paths to `insert`. */
async function attachImages(files, insert, m = null) {
  if (!files.length) return;
  toast(files.length > 1 ? `Uploading ${files.length} images…` : 'Uploading image…', { ms: 0 });
  try {
    const paths = await uploadImages(files, m);
    insert(asArgs(paths));
    toast(paths.length > 1 ? `${paths.length} images added` : 'Image added');
  } catch (e) {
    toast(e?.status === 413 ? 'That image is too large' : explain(e), { error: true });
  }
}

/** Typed into the session like a pasted path: Claude Code takes an image path as the image. */
const intoTerm = (args) => term?.paste(`${args} `);

// -- service worker --------------------------------------------------------------

function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  let reloading = false;
  // A tapped notification while the app is open.
  navigator.serviceWorker.addEventListener('message', (e) => {
    if (e.data?.type !== 'open') return;
    const go = new URLSearchParams(new URL(e.data.url).hash.slice(1)).get('go');
    if (!go) return;
    if (config && app.dataset.view !== 'login') openGo(go);
    else linkGo = go;
  });
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (reloading) location.reload();
  });
  navigator.serviceWorker
    .register('/sw.js')
    .then((reg) => {
      const offer = (worker) =>
        toast('New version ready. Tap to reload.', {
          ms: 0,
          onClick: () => {
            reloading = true;
            worker.postMessage('skip-waiting');
          },
        });
      if (reg.waiting && navigator.serviceWorker.controller) offer(reg.waiting);
      reg.addEventListener('updatefound', () => {
        const worker = reg.installing;
        worker?.addEventListener('statechange', () => {
          if (worker.state === 'installed' && navigator.serviceWorker.controller) offer(worker);
        });
      });
      document.addEventListener('visibilitychange', () => document.visibilityState === 'visible' && reg.update().catch(() => {}));
    })
    .catch(() => {});
}

// -- wiring -------------------------------------------------------------------------

function wire() {
  $('#unlock').addEventListener('click', unlock);
  $('#enroll-go').addEventListener('click', doEnroll);
  $('#enroll-token').addEventListener('input', (e) => {
    const v = e.target.value.trim();
    if (v.length >= 16) enroll.prefetch(v).catch((err) => loginMessage(explain(err), true));
  });
  $('#back').addEventListener('click', () => closeTerm());
  $('#act-new').addEventListener('click', () => dash.newSheet());
  $('#act-inbox').addEventListener('click', () => {
    store('inboxSeen', Math.floor(Date.now() / 1000));
    $('#inbox-badge').hidden = true;
    dash.inboxSheet();
  });
  $('#act-herdr').addEventListener('click', () => {
    const s = dash.currentSession();
    openTerm({ k: 'herdr', s: s?.name, title: `herdr · ${s?.name || 'default'}`, m: dash.machine || undefined });
  });
  $('#act-tmux').addEventListener('click', () => {
    const name = dash.state.tmux?.default;
    if (name) openTerm({ k: 'tmux', t: name, title: name, m: dash.machine || undefined });
  });
  $('#act-menu').addEventListener('click', () => (app.dataset.view === 'term' ? termMenu() : dashMenu()));
  $('#title').addEventListener('click', () => app.dataset.view === 'dash' && machinesSheet(dash));
  $('#term-reconnect').addEventListener('click', () => term?.reconnect());
  window.addEventListener('popstate', () => {
    if (term && history.state?.term !== true) closeTerm(true);
  });
  onViewport(() => term?.scheduleFit());

  keybar = new KeyBar($('#keys'), {
    send: (s) => term?.sendText(s),
    appCursor: () => term?.appCursor() ?? false,
    // Paste: a copied screenshot or photo goes to the agent as an image; text as text.
    paste: () =>
      readClipboard()
        .then(({ images, text }) => (images.length ? attachImages(images, intoTerm, target?.m) : text && term?.paste(text)))
        .catch(() => toast('Clipboard not available', { error: true })),
    attach: () => pickImages().then((files) => attachImages(files, intoTerm, target?.m)),
    blur: () => term?.blur(),
    dictation: false,
    onMicDown: (btn) => micDown(btn, termSink),
    onMicUp: () => micUp(),
  }, $('#keys-end'));

  dash = new Dashboard($('#dash'), {
    openTerm,
    dictateInto: null,
    attachInto: (insert) => pickImages().then((files) => attachImages(files, insert, dash.machine)),
    onMachine: () => app.dataset.view === 'dash' && setView('dash'),
  });
  events = new Events({
    onState: (state) => {
      dash.update(state);
      refreshUpdates();
      machinesChanged(state);
      updateBadges(state);
    },
    onAlert: (a) => {
      const text = a.pushTitle || (a.kind === 'blocked' ? `${a.title}: needs you` : '');
      if (app.dataset.view === 'dash' && text) toast(text);
    },
    onAuth: onExpired,
    onConn: setConn,
  });
}

async function boot(first = true) {
  if (first) {
    initViewport();
    wire();
    registerServiceWorker();
  }
  try {
    info = await api('/auth/info');
  } catch {
    return showOffline();
  }
  if (!info.loggedIn) return showLogin();
  await enterApp();
}

boot();
