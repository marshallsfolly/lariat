// The machines sheet (tap the name at the top left): switch between this
// machine and the ones it is the hub for, add one (a code for `lariat join`),
// or hold one for its agent alerts and to remove it.

import { api, at } from './api.js';
import { openSheet, closeSheet, item, items, section, note } from './sheet.js';
import { el, glyph, G, toast } from './util.js';
import { pressable } from './dashboard.js';

let watch = null; // while "Add a machine" waits: (state) => void

/** Called with each new state: notices the machine being added. */
export function machinesChanged(state) {
  watch?.(state);
}

export function machinesSheet(dash) {
  const here = dash.hubConfig?.appName || 'this machine';
  const list = [{ name: null, label: here, connected: true, state: dash.hubState }, ...dash.others().map((m) => ({ ...m, label: m.name }))];
  openSheet({
    title: 'Machines',
    sub: list.length > 1 ? 'Your phone reaches them through this one. Hold one for more.' : 'Add the other machines you run agents on. Hold this one for its agent alerts.',
    body: [
      el('div', { class: 'items' }, ...list.map((m) => machineRow(dash, m))),
      items(item({ icon: G.plus, label: 'Add a machine', close: false, onSelect: () => addMachine(dash) })),
    ],
  });
}

function machineRow(dash, m) {
  const version = m.state?.update?.current;
  const status = m.name === null ? (version ? `lariat ${version} · this machine` : 'this machine') : m.connected ? (version ? `lariat ${version}` : 'connected') : 'not reachable';
  const on = (m.name || null) === (dash.machine || null);
  const row = el(
    'button',
    { class: `item machine-row${on ? ' on' : ''}${m.connected ? ' live' : ''}`, type: 'button' },
    glyph(G.server),
    el('span', { class: 'label' }, el('span', { text: m.label }), el('span', { class: 'meta', text: status })),
    on ? glyph(G.check, 'mark') : null,
  );
  pressable(row, {
    onTap: () => {
      closeSheet();
      dash.selectMachine(m.name);
    },
    onHold: () => machineSheet(m),
  });
  return row;
}

const AGENTS = [
  ['claude', 'Claude Code'],
  ['grok', 'Grok Build'],
];

/** One machine: its agents' alerts (the hooks that report to lariat), and Remove. */
async function machineSheet(m) {
  const name = m.name;
  let hooks = null;
  if (m.connected) hooks = await api(at(name, '/api/hooks')).catch(() => null);
  const install = (agent, title) =>
    api(at(name, '/api/hooks'), { method: 'POST', body: { agent } })
      .then(() => {
        toast(`${title} alerts on: new sessions report`);
        machineSheet(m);
      })
      .catch((e) => toast(e?.message || String(e), { error: true }));
  const alerts = !m.connected
    ? [note('Not reachable right now.')]
    : !hooks
      ? [note('Needs a newer lariat there (⋮ → Updates).')]
      : [
          ...AGENTS.map(([agent, title]) =>
            hooks[agent]
              ? el('div', { class: 'setting info' }, glyph(G.check), el('span', { class: 'label' }, el('span', { text: title }), el('span', { class: 'meta', text: 'alerts on' })))
              : item({ icon: G.bell, label: `${title}: turn alerts on`, close: false, onSelect: () => install(agent, title) }),
          ),
          note(hooks.managed ? 'Claude Code alerts come from your NixOS configuration here.' : 'Adds lariat\'s hooks to the agent\'s settings there (yours are kept); agents you install later: come back here.'),
        ];
  openSheet({
    title: m.label,
    body: [
      section('Agent alerts', ...alerts),
      name &&
        items(
          item({
            icon: G.trash,
            label: `Remove ${name}`,
            danger: true,
            hold: true,
            onSelect: () =>
              api('/api/machines/remove', { method: 'POST', body: { name } })
                .then((r) => toast(r.unlinked ? `${name} removed` : `${name} removed (it was off: revoke the link there later)`))
                .catch((e) => toast(e?.message || String(e), { error: true })),
          }),
        ),
      name && note('Remove: it stops showing here and its link to this machine is revoked; lariat keeps running there.'),
    ].filter(Boolean),
  });
}

async function addMachine(dash) {
  let res;
  try {
    res = await api('/api/machines/invite', { method: 'POST', body: {} });
  } catch (e) {
    toast(e?.message || String(e), { error: true });
    return;
  }
  const before = new Set(dash.others().map((m) => m.name));
  const copy = () =>
    navigator.clipboard
      .writeText(res.command)
      .then(() => toast('Copied'))
      .catch(() => toast('Copy it by hand', { error: true }));
  openSheet({
    title: 'Add a machine',
    sub: 'Run this on the new machine:',
    body: [
      el('code', { class: 'join-cmd', text: res.command }),
      items(item({ icon: G.copy, label: 'Copy', close: false, onSelect: copy })),
      note(`The code works once, for ${Math.round(res.expires / 60)} minutes; the machine shows up here when it has joined. No lariat there yet: one download from github.com/marshallsfolly/lariat, then \`lariat service install\`.`),
    ],
    onClose: () => (watch = null),
  });
  watch = (state) => {
    const added = (state.machines || []).find((m) => !before.has(m.name));
    if (!added) return;
    watch = null;
    closeSheet();
    toast(`${added.name} added`);
    dash.selectMachine(added.name);
  };
}
