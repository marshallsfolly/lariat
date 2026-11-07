// Dictation, transcribed on the server by whisper.cpp.
//
// Streaming (AudioWorklet): 16 kHz PCM goes to /ws/dictate in 50 ms chunks,
// partial text comes back about ten times a second and the final text right
// after you stop. With `pauseMs` set, a pause after speech ends the
// recording by itself. Browsers without AudioWorklet record with MediaRecorder
// and upload the clip to /api/dictate. Either way the microphone is released
// as soon as recording stops, so the iOS mic indicator goes away.
//
// iOS starts Web Audio only on a tap's *end* (touchdown does not count) or
// while the page is capturing. So there is one AudioContext for the page:
// primeAudio() creates and starts it on any tap, it is suspended between
// recordings, and a recording resumes it only after the microphone is open.
// Every wait has a timeout: a recording never hangs in "starting".

import { api } from './api.js';

const MAX_MS = 60_000;
const START_TIMEOUT_MS = 2500;
const FINAL_TIMEOUT_MS = 10_000;
const VOICE_FLOOR = 0.012; // RMS that counts as voice, at least
const VOICE_OVER_NOISE = 3; // ...and this many times the noise floor
const VOICE_MIN_CHUNKS = 3; // 150 ms of voice before a pause can end it

let ctx = null;
let workletReady = null;
let primed = false; // the context has run once, so it may resume without a tap
let wanted = 0; // recordings that want the context running

/** Call from tap handlers (pointerup/click/keydown): lets audio start later without one. */
export function primeAudio() {
  if (!window.AudioWorkletNode) return;
  if (!ctx || ctx.state === 'closed') {
    try {
      ctx = new AudioContext();
    } catch {
      ctx = null;
      return;
    }
    primed = false;
    // iOS "interrupts" audio (calls, Siri, backgrounding); after that it needs a tap again.
    ctx.onstatechange = () => ctx?.state === 'interrupted' && (primed = false);
    workletReady = ctx.audioWorklet.addModule('/js/pcm-worklet.js');
    workletReady.catch(() => {});
  }
  if (ctx.state === 'running' || (primed && !wanted)) return;
  ctx
    .resume()
    .then(() => {
      primed = true;
      if (!wanted) ctx.suspend().catch(() => {});
    })
    .catch(() => {});
}

function timeout(promise, ms, message) {
  return Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(new Error(message)), ms))]);
}

export class Recorder {
  constructor() {
    this.mode = null; // 'stream' | 'upload' while recording
    this.stream = null;
    this.nodes = [];
    this.ws = null;
    this.queue = [];
    this.final = null;
    this.rec = null;
    this.chunks = [];
    this.timers = [];
  }

  get active() {
    return this.mode !== null;
  }

  static supported() {
    return !!(navigator.mediaDevices?.getUserMedia && (window.AudioWorkletNode || window.MediaRecorder));
  }

  /**
   * Start recording.
   * hooks: onTick(seconds), onLimit(), onPartial(text), onLevel(0..1), onError(err),
   * pauseMs + onPause() (a pause after speech; 0 = off), onPauseProgress(0..1).
   */
  async start(hooks = {}) {
    if (this.active) return;
    if (window.AudioWorkletNode) await this.startStream(hooks);
    else await this.startUpload();
    const started = Date.now();
    this.timers = [
      setInterval(() => hooks.onTick?.(Math.floor((Date.now() - started) / 1000)), 250),
      setTimeout(() => hooks.onLimit?.(), MAX_MS),
    ];
    hooks.onTick?.(0);
  }

  async startStream(hooks) {
    this.mode = 'stream';
    this.failed = null;
    wanted++;
    let settle;
    this.final = new Promise((resolve, reject) => (settle = { resolve, reject }));
    this.final.catch(() => {});
    this.queue = [];
    this.partial = '';
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/dictate`);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    const fail = (err) => {
      settle.reject(err);
      if (this.ws !== ws || this.mode !== 'stream' || this.failed) return;
      this.failed = err;
      if (this.nodes.length) hooks.onError?.(err); // still starting: start() throws it instead
    };
    ws.onopen = () => {
      for (const m of this.queue) ws.send(m);
      this.queue = null;
    };
    ws.onmessage = (e) => {
      let m;
      try {
        m = JSON.parse(e.data);
      } catch {
        return;
      }
      if (m.t === 'partial') {
        this.partial = m.text;
        hooks.onPartial?.(m.text);
      } else if (m.t === 'final') {
        settle.resolve(m.text || '');
        if (m.limit) hooks.onLimit?.();
      } else if (m.t === 'error') fail(new Error(m.message || 'Dictation failed'));
    };
    ws.onclose = () => fail(new Error('Dictation connection lost'));
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      primeAudio();
      await timeout(workletReady, START_TIMEOUT_MS, 'Audio did not start');
      // Capturing now, so iOS lets the context run even without a tap.
      await timeout(ctx.resume(), START_TIMEOUT_MS, 'The microphone did not start. Tap the mic again.');
      if (this.failed) throw this.failed;
      if (this.mode !== 'stream') throw new Error('Cancelled');
    } catch (e) {
      this.cancel();
      throw e;
    }
    primed = true;
    const src = ctx.createMediaStreamSource(this.stream);
    const node = new AudioWorkletNode(ctx, 'pcm16k');
    // The worklet only has to run, not be heard: route it into a muted gain.
    const mute = ctx.createGain();
    mute.gain.value = 0;
    src.connect(node).connect(mute).connect(ctx.destination);
    this.nodes = [src, node, mute];

    // Pause detection: voice is RMS well above an adaptive noise floor.
    let floor = 0.004;
    let voiced = 0;
    let lastVoice = 0;
    node.port.onmessage = (e) => {
      const { pcm, peak, rms, last } = e.data;
      if (pcm.byteLength) this.send(pcm);
      if (last) this.flushed?.();
      hooks.onLevel?.(peak);
      const now = performance.now();
      const voice = rms > Math.max(VOICE_FLOOR, floor * VOICE_OVER_NOISE);
      // Follows quiet stretches down at once and noise up slowly (slower still while talking).
      floor = rms < floor ? rms : floor + (rms - floor) * (voice ? 0.005 : 0.05);
      if (voice) {
        voiced++;
        lastVoice = now;
      }
      if (!hooks.pauseMs || voiced < VOICE_MIN_CHUNKS || !this.partial) return hooks.onPauseProgress?.(0);
      const quiet = voice ? 0 : (now - lastVoice) / hooks.pauseMs;
      hooks.onPauseProgress?.(Math.min(1, quiet));
      if (quiet >= 1 && this.mode === 'stream') hooks.onPause?.();
    };
  }

  send(data) {
    if (this.queue) this.queue.push(data);
    else if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(data);
  }

  async startUpload() {
    this.mode = 'upload';
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true } });
    } catch (e) {
      this.mode = null;
      throw e;
    }
    const types = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
    const mimeType = types.find((t) => MediaRecorder.isTypeSupported?.(t));
    this.rec = new MediaRecorder(this.stream, mimeType ? { mimeType } : undefined);
    this.chunks = [];
    this.rec.ondataavailable = (e) => e.data.size && this.chunks.push(e.data);
    this.rec.start(1000);
  }

  /** Stop and transcribe. Resolves to the text. */
  async stop() {
    const mode = this.mode;
    this.clearTimers();
    if (mode === 'stream') {
      // Collect the worklet's last partial chunk before the mic goes away.
      const node = this.nodes[1];
      await new Promise((resolve) => {
        this.flushed = resolve;
        node ? node.port.postMessage('flush') : resolve();
        setTimeout(resolve, 150);
      });
      this.flushed = null;
      this.releaseAudio();
      this.send(JSON.stringify({ t: 'stop' }));
      const final = this.final;
      const ws = this.ws;
      this.mode = null;
      try {
        return await timeout(final, FINAL_TIMEOUT_MS, 'Transcription timed out');
      } finally {
        ws?.close();
        if (this.ws === ws) this.ws = null;
      }
    }
    if (mode === 'upload') {
      const rec = this.rec;
      const stopped = new Promise((r) => rec.addEventListener('stop', r, { once: true }));
      if (rec.state !== 'inactive') rec.stop();
      await stopped;
      this.releaseAudio();
      this.mode = null;
      const blob = new Blob(this.chunks, { type: (rec.mimeType || 'audio/mp4').split(';')[0] });
      this.chunks = [];
      if (blob.size < 512) return '';
      const res = await api('/api/dictate', { method: 'POST', body: blob });
      return res.text || '';
    }
    return '';
  }

  cancel() {
    this.clearTimers();
    if (this.ws) {
      this.send(JSON.stringify({ t: 'cancel' }));
      this.ws.close();
      this.ws = null;
    }
    if (this.rec && this.rec.state !== 'inactive') this.rec.stop();
    this.releaseAudio();
    this.chunks = [];
    this.mode = null;
  }

  clearTimers() {
    clearInterval(this.timers[0]);
    clearTimeout(this.timers[1]);
    this.timers = [];
  }

  releaseAudio() {
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    if (this.nodes[1]) this.nodes[1].port.onmessage = null;
    for (const n of this.nodes) n.disconnect();
    this.nodes = [];
    if (this.mode === 'stream') {
      wanted = Math.max(0, wanted - 1);
      if (!wanted) ctx?.suspend().catch(() => {});
    }
    this.rec = null;
  }
}
