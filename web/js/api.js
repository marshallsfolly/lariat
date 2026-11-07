// fetch() wrapper: same-origin, never cached, CSRF header on writes.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function api(path, { method = 'GET', body, headers = {}, keepalive = false } = {}) {
  const opts = { method, headers: { ...headers }, credentials: 'same-origin', cache: 'no-store', keepalive };
  if (method !== 'GET') opts.headers['X-Lariat'] = '1';
  if (body instanceof Blob) {
    opts.body = body;
    opts.headers['Content-Type'] = body.type || 'application/octet-stream';
  } else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers['Content-Type'] = 'application/json';
  }
  let r;
  try {
    r = await fetch(path, opts);
  } catch {
    throw new ApiError(0, 'unreachable');
  }
  const text = await r.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!r.ok) throw new ApiError(r.status, (data && data.error) || r.statusText || 'request failed');
  return data;
}

// Other machines are reached through this one (the hub): /m/<name>/api/..., /m/<name>/ws/term.
let machine = null;
export function setMachine(name) {
  machine = name || null;
}
/** `path` on machine `m` (null: this one). */
export const at = (m, path) => (m ? `/m/${encodeURIComponent(m)}${path}` : path);
/** `path` on the machine the dashboard shows. */
export const here = (path) => at(machine, path);

export const action = (op, params = {}) => api(here('/api/action'), { method: 'POST', body: { op, ...params } });

/**
 * The live event stream. Calls onState(state) with the whole dashboard state
 * on every change, onAlert(item) for new ntfy messages, and onAuth() when the
 * session is gone (expired or the server restarted).
 */
export class Events {
  constructor({ onState, onAlert, onAuth, onConn }) {
    Object.assign(this, { onState, onAlert, onAuth, onConn });
    this.es = null;
    this.retry = 0;
    this.timer = 0;
  }

  start() {
    this.stop();
    this.onConn?.('wait');
    const es = new EventSource('/api/events');
    this.es = es;
    es.addEventListener('state', (e) => {
      this.retry = 0;
      this.onConn?.('up');
      this.onState(JSON.parse(e.data));
    });
    es.addEventListener('alert', (e) => this.onAlert?.(JSON.parse(e.data)));
    es.addEventListener('expired', () => {
      this.stop();
      this.onAuth();
    });
    es.onerror = () => {
      this.onConn?.('down');
      if (es.readyState === EventSource.CLOSED) this.recover();
    };
  }

  async recover() {
    this.stop();
    try {
      await api('/api/state');
    } catch (e) {
      if (e.status === 401) {
        this.onAuth();
        return;
      }
    }
    this.retry = Math.min(this.retry + 1, 6);
    this.timer = setTimeout(() => this.start(), 500 * 2 ** this.retry);
  }

  stop() {
    clearTimeout(this.timer);
    if (this.es) {
      this.es.close();
      this.es = null;
    }
  }
}
