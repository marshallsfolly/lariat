// Updates for every machine, from the phone: what each runs, whether a newer
// release is out, "Update now", and whether it installs updates on its own.
// Each machine reports its update state with the rest of its state; the sheet
// redraws as that changes (downloading, waiting for terminals, restarting).

import { api, at } from './api.js';
import { openSheet, item, section, choice, note } from './sheet.js';
import { el, glyph, G, toast } from './util.js';

const BUSY = new Set(['checking', 'downloading', 'installing', 'restarting']);

const STATUS = {
  checking: () => 'checking…',
  downloading: (u) => `downloading ${u.latest}…`,
  waiting: (u) => `${u.latest} ready: installs once its terminals close`,
  installing: () => 'installing…',
  restarting: () => 'restarting…',
  restart: (u) => `${u.installed} installed: restart lariat`,
  error: () => 'update failed',
};

function statusText(u) {
  if (STATUS[u.status]) return STATUS[u.status](u);
  if (u.kind === 'nix') return 'updated with Nix';
  if (u.available) return `${u.latest} available`;
  if (u.latest) return 'up to date';
  return u.mode === 'off' ? 'not checking' : 'not checked yet';
}

/** Machines needing attention: a newer release that is not installing itself. */
export function pendingUpdates(machines) {
  return machines.filter(({ state }) => {
    const u = state?.update;
    return u?.available && !(u.mode === 'auto' && u.canApply);
  }).length;
}

let view = null; // { render, key } while the sheet is open

/** Redraw the open sheet when a machine's update state changed. */
export function refreshUpdates() {
  view?.render();
}

/**
 * @param {() => Array<{name: string|null, label: string, connected: boolean, state: object}>} machines
 */
export function updatesSheet(machines) {
  const body = el('div', { class: 'updates' });
  const send = (m, req) =>
    api(at(m, '/api/update'), { method: 'POST', body: req }).catch((e) => toast(e?.message || String(e), { error: true }));
  const render = () => {
    const list = machines();
    const key = JSON.stringify(list.map((m) => [m.name, m.connected, m.state?.update]));
    if (view && key === view.key) return;
    if (view) view.key = key;
    body.replaceChildren(...list.map((m) => machineSection(m, send)));
  };
  view = { render, key: '' };
  render();
  openSheet({
    title: 'Updates',
    sub: 'Auto: installed once no terminal is open',
    body: [body],
    onClose: () => (view = null),
  });
}

/** The version, with what is going on under it. */
function info(icon, label, value) {
  return el('div', { class: 'setting info' }, glyph(icon), el('span', { class: 'label' }, el('span', { text: label }), el('span', { class: 'meta', text: value })));
}

function machineSection(m, send) {
  const u = m.state?.update;
  const rows = [];
  if (!m.connected) {
    rows.push(note('Not reachable right now.'));
  } else if (!u) {
    rows.push(note('It does not report updates: run `lariat update` there once, then it updates from here.'));
  } else {
    const busy = BUSY.has(u.status);
    rows.push(info(G.server, `lariat ${u.current}`, statusText(u)));
    if (u.status === 'error' && u.error) rows.push(note(u.error));
    if (u.kind === 'nix') {
      rows.push(note('Installed with Nix: it updates with your flake input (nix flake update lariat, then rebuild).'));
    } else {
      if (u.available && u.canApply && !busy) {
        const now = u.status === 'waiting'; // installing now ends its open terminals
        rows.push(
          item({
            icon: G.download,
            label: now ? 'Update now' : `Update to ${u.latest}`,
            hold: now,
            close: false,
            onSelect: () => send(m.name, { op: 'apply' }).then(() => m.name === null && toast('lariat restarts after the update: unlock again then')),
          }),
        );
      }
      if (u.available && !u.canApply) rows.push(note(`Installed with pip: install the ${u.latest} wheel from the release page there.`));
      if (!busy) rows.push(item({ icon: G.refresh, label: 'Check now', close: false, onSelect: () => send(m.name, { op: 'check' }) }));
      const options = u.canApply
        ? [
            ['auto', 'Auto'],
            ['notify', 'Notify'],
            ['off', 'Off'],
          ]
        : [
            ['notify', 'Notify'],
            ['off', 'Off'],
          ];
      rows.push(choice({ icon: G.update, label: 'New releases', options, value: u.mode, onChange: (mode) => send(m.name, { op: 'mode', mode }) }));
    }
  }
  return section(m.label, ...rows);
}
