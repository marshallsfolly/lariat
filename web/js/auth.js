// Passkey login and enrollment (WebAuthn), talking to /auth/*.
//
// Options are fetched *before* the button is tapped and refreshed while the
// screen is up, so navigator.credentials.* runs directly inside the tap:
// Safari wants a fresh user gesture for passkey prompts.

import { api } from './api.js';

const b64uToBuf = (s) => {
  const b64 = s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
};

const bufToB64u = (buf) => {
  const bytes = new Uint8Array(buf);
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
};

function requestOptions(json) {
  const o = { ...json, challenge: b64uToBuf(json.challenge) };
  o.allowCredentials = (json.allowCredentials || []).map((c) => ({ ...c, id: b64uToBuf(c.id) }));
  return o;
}

function creationOptions(json) {
  const o = { ...json, challenge: b64uToBuf(json.challenge), user: { ...json.user, id: b64uToBuf(json.user.id) } };
  o.excludeCredentials = (json.excludeCredentials || []).map((c) => ({ ...c, id: b64uToBuf(c.id) }));
  if (!o.hints) delete o.hints;
  return o;
}

function credentialJSON(cred) {
  const r = cred.response;
  const response = { clientDataJSON: bufToB64u(r.clientDataJSON) };
  if (r.attestationObject) {
    response.attestationObject = bufToB64u(r.attestationObject);
    response.transports = typeof r.getTransports === 'function' ? r.getTransports() : [];
  } else {
    response.authenticatorData = bufToB64u(r.authenticatorData);
    response.signature = bufToB64u(r.signature);
    if (r.userHandle) response.userHandle = bufToB64u(r.userHandle);
  }
  return {
    id: cred.id,
    rawId: bufToB64u(cred.rawId),
    type: cred.type,
    response,
    authenticatorAttachment: cred.authenticatorAttachment || undefined,
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
  };
}

const FRESH_MS = 90_000;

/** Keeps a fresh set of login options so the tap can go straight to Face ID. */
export class Login {
  constructor() {
    this.options = null;
    this.fetched = 0;
    this.timer = 0;
  }

  async prefetch() {
    clearTimeout(this.timer);
    try {
      this.options = await api('/auth/login/options', { method: 'POST' });
      this.fetched = Date.now();
    } catch (e) {
      this.options = null;
      throw e;
    } finally {
      this.timer = setTimeout(() => this.prefetch().catch(() => {}), FRESH_MS);
    }
  }

  stop() {
    clearTimeout(this.timer);
  }

  async run() {
    if (!this.options || Date.now() - this.fetched > FRESH_MS) await this.prefetch();
    const options = this.options;
    this.options = null; // single use
    const cred = await navigator.credentials.get({ publicKey: requestOptions(options), mediation: 'optional' });
    await api('/auth/login/verify', { method: 'POST', body: { credential: credentialJSON(cred) } });
    this.stop();
  }
}

/** Enrollment with the one-time token; prefetches once the token looks complete. */
export class Enroll {
  constructor() {
    this.options = null;
    this.forToken = '';
  }

  async prefetch(token) {
    if (token.length < 16) return;
    this.options = await api('/auth/register/options', { method: 'POST', headers: { 'X-Enroll-Token': token } });
    this.forToken = token;
  }

  async run(token, label) {
    if (!this.options || this.forToken !== token) await this.prefetch(token);
    const options = this.options;
    this.options = null;
    const cred = await navigator.credentials.create({ publicKey: creationOptions(options) });
    return api('/auth/register/verify', {
      method: 'POST',
      headers: { 'X-Enroll-Token': token },
      body: { credential: credentialJSON(cred), label },
    });
  }
}

export const logout = () => api('/auth/logout', { method: 'POST' });

export function webauthnSupported() {
  return typeof window.PublicKeyCredential === 'function' && !!navigator.credentials;
}
