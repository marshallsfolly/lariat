// Web Push for the installed app: alerts arrive while it is closed, and
// tapping one opens that agent. Needs a service worker, the Push API and, on
// iOS (16.4+), the app added to the Home Screen. Permission can only be asked
// from a tap. While the app is in front it reports so, and the server shows
// the alert in the app instead of pushing it.

import { api } from './api.js';
import { storage, store } from './util.js';

export function deviceId() {
  let id = storage('device', '');
  if (!id) {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    id = btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    store('device', id);
  }
  return id;
}

/** {ok} or {ok: false, why} */
export function pushSupport() {
  if ('serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window) return { ok: true };
  const ios = /iPhone|iPad/.test(navigator.userAgent);
  const installed = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  return { ok: false, why: ios && !installed ? 'Add the app to your Home Screen to get notifications.' : 'This browser has no Web Push.' };
}

export async function currentSubscription() {
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

function keyBytes(b64u) {
  const s = atob(b64u.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (b64u.length % 4)) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

export const pushKinds = () => storage('pushKinds', ['blocked', 'done']);

/** Call straight from the tap: the permission prompt must come first. */
export async function enablePush(key) {
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    throw new Error(permission === 'denied' ? 'Notifications are off for this app in Settings' : 'Notifications not allowed');
  }
  const reg = await navigator.serviceWorker.ready;
  const sub =
    (await reg.pushManager.getSubscription()) ||
    (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) }));
  await api('/api/push/subscribe', { method: 'POST', body: { subscription: sub.toJSON(), device: deviceId(), kinds: pushKinds() } });
}

/** Tell the server which kinds this device wants (after a settings change). */
export async function updatePushKinds(kinds) {
  store('pushKinds', kinds);
  const sub = await currentSubscription();
  if (sub) await api('/api/push/subscribe', { method: 'POST', body: { subscription: sub.toJSON(), device: deviceId(), kinds } });
}

export async function disablePush() {
  const sub = await currentSubscription();
  await sub?.unsubscribe();
  await api('/api/push/unsubscribe', { method: 'POST', body: { device: deviceId() } });
}

export const testPush = () => api('/api/push/test', { method: 'POST', body: { device: deviceId() } });

let beat = 0;
export function trackPresence() {
  const report = (visible) =>
    api('/api/presence', { method: 'POST', body: { device: deviceId(), visible }, keepalive: true }).catch(() => {});
  // The gateway counts a "visible" for 15 s: a lock that never reports
  // "hidden" (iOS can suspend first) then holds back alerts for 15 s at most.
  const update = () => {
    clearInterval(beat);
    const visible = document.visibilityState === 'visible';
    report(visible);
    if (visible) beat = setInterval(() => report(true), 10_000);
  };
  document.addEventListener('visibilitychange', update);
  window.addEventListener('pagehide', () => {
    clearInterval(beat);
    report(false);
  });
  update();
}
