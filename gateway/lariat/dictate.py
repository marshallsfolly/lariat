"""Local dictation: whisper.cpp's whisper-server, kept warm on loopback.

The gateway starts the server when it starts and warms it up (the first run
compiles GPU kernels, which can take seconds), sends it 16 kHz mono 16-bit
PCM as WAV, and restarts it if it dies. With `dictate_idle_minutes` > 0 it is
stopped after that long unused, freeing its memory, and started again on the
next recording.

Browsers stream PCM over /ws/dictate and get partial transcripts while they
record (back to back: the next one starts as soon as there is 0.1 s of new
audio, about ten a second on a GPU), then the final text right after they
stop. /api/dictate takes a
finished recording in any format ffmpeg reads, for browsers without
AudioWorklet. Audio and text are never logged. The server answers one
request at a time; partials skip a turn rather than queue.
"""

from __future__ import annotations

import asyncio
import collections
import io
import json
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
import wave
from array import array

import aiohttp
from aiohttp import web

from .config import Config

log = logging.getLogger("lariat.dictate")

RATE = 16000
BYTES_PER_SECOND = RATE * 2
MAX_UPLOAD = 2 * 1024 * 1024
TIMEOUT = 30.0  # one transcription
START_TIMEOUT = 90.0  # model load plus the first run (shader compile on a new driver)
PARTIAL_MIN_NEW = BYTES_PER_SECOND // 10  # new audio (0.1 s) that triggers the next partial
REVIVE_AFTER = 3.0  # seconds before restarting a server that died
SILENCE_PEAK = 400  # of 32767: quieter clips are not sent to the model at all
MAX_STREAMS = 3
# whisper.cpp marks silence and noise like "[BLANK_AUDIO]" or "(wind blowing)".
NOISE = re.compile(r"^\s*[\[(][^\])]*[\])]\s*$")


class DictationError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def is_silent(pcm: bytes) -> bool:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return not samples or max(max(samples), -min(samples)) < SILENCE_PEAK


def clean(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines()]
    return " ".join(ln for ln in lines if ln and not NOISE.match(ln)).strip()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Dictation:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.proc: asyncio.subprocess.Process | None = None
        self.url: str | None = None
        self.starting: asyncio.Task | None = None
        self.lock = asyncio.Lock()  # one transcription at a time
        self.http: aiohttp.ClientSession | None = None
        self.idle: asyncio.TimerHandle | None = None
        self.stderr: collections.deque[str] = collections.deque(maxlen=30)
        self.streams = 0
        self.closing = False

    @property
    def ready(self) -> bool:
        return self.url is not None and self.proc is not None and self.proc.returncode is None

    # -- the server process -------------------------------------------------

    async def ensure(self) -> str:
        """Start the server if needed and wait until it is warm. Returns its URL."""
        if not self.ready:
            if self.starting is None or self.starting.done():
                self.starting = asyncio.create_task(self._start())
            await asyncio.shield(self.starting)  # many waiters, one start
        self._touch()
        assert self.url is not None
        return self.url

    async def warm(self) -> None:
        """Start and warm up in the background (at gateway start); failures are only logged."""
        try:
            await self.ensure()
        except DictationError:
            pass  # _start logged why; the next recording tries again

    async def _start(self) -> None:
        await self._kill()
        if self.http is None:
            self.http = aiohttp.ClientSession()
        port = free_port()
        argv = [
            self.cfg.whisper or "whisper-server",
            "-m", self.cfg.whisper_model or "",
            "--host", "127.0.0.1",
            "--port", str(port),
            "-l", self.cfg.dictate_language,
            "-t", str(self.cfg.dictate_threads),
            "-bo", "1",
            "-bs", "1",
            "-nt",
            "-sns",
            *self.cfg.dictate_args,
        ]  # fmt: skip
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stderr.clear()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cfg.runtime_dir,
            )
        except OSError as e:
            log.warning("dictation: can't start whisper-server: %s", e)
            raise DictationError("transcriber unavailable", 503) from e
        self.proc = proc
        asyncio.create_task(self._drain(proc))
        url = f"http://127.0.0.1:{port}"
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            async with asyncio.timeout(START_TIMEOUT):
                while True:
                    if proc.returncode is not None:
                        raise DictationError("transcriber exited", 503)
                    try:
                        async with self.http.get(url + "/", timeout=aiohttp.ClientTimeout(total=2)) as r:
                            if r.status == 200:
                                break
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        pass
                    await asyncio.sleep(0.05)
                # The first run compiles GPU kernels; do it now, not on the user's words.
                await self._post(url, bytes(BYTES_PER_SECOND // 2), partial=True)
        except (DictationError, TimeoutError, aiohttp.ClientError) as e:
            log.warning("dictation: whisper-server did not come up: %s\n%s", e, self._errors())
            await self._kill()
            raise DictationError("transcriber failed to start", 503) from e
        self.url = url
        log.info("dictation: whisper-server ready on 127.0.0.1:%d in %.1fs", port, loop.time() - started)
        asyncio.create_task(self._watch(proc))

    async def _drain(self, proc: asyncio.subprocess.Process) -> None:
        # whisper-server logs per request; keep a short tail in memory for errors only.
        assert proc.stderr is not None
        while line := await proc.stderr.readline():
            self.stderr.append(line.decode(errors="replace").rstrip())

    def _errors(self) -> str:
        return "\n".join(ln for ln in self.stderr if re.search(r"error|fail|unable|cannot", ln, re.I))

    async def _watch(self, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        if self.proc is proc:  # not stopped by us
            log.warning("dictation: whisper-server exited (%s)\n%s", code, self._errors())
            self.proc = None
            self.url = None
            if self.cfg.dictate_idle_minutes <= 0 and not self.closing:
                await asyncio.sleep(REVIVE_AFTER)
                if not self.closing and self.proc is None:
                    await self.warm()

    def _touch(self) -> None:
        if self.idle:
            self.idle.cancel()
            self.idle = None
        if self.cfg.dictate_idle_minutes > 0:
            delay = self.cfg.dictate_idle_minutes * 60
            self.idle = asyncio.get_running_loop().call_later(delay, self._on_idle)

    def _on_idle(self) -> None:
        self.idle = None
        if self.lock.locked() or self.streams:
            self._touch()
            return
        if self.proc is not None:
            log.info("dictation: idle, stopping whisper-server")
            asyncio.create_task(self._kill())

    async def _kill(self) -> None:
        proc, self.proc, self.url = self.proc, None, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def close(self) -> None:
        self.closing = True
        if self.idle:
            self.idle.cancel()
        if self.starting and not self.starting.done():
            self.starting.cancel()
        await self._kill()
        if self.http:
            await self.http.close()
            self.http = None

    # -- transcription --------------------------------------------------------

    async def _post(self, url: str, pcm: bytes, partial: bool) -> str:
        assert self.http is not None
        form = aiohttp.FormData()
        form.add_field("file", to_wav(pcm), filename="audio.wav", content_type="audio/wav")
        form.add_field("response_format", "json")
        form.add_field("temperature", "0")
        # The server keeps request fields between requests, so send all of them every time.
        form.add_field("temperature_inc", "0" if partial else "0.2")
        form.add_field("prompt", self.cfg.dictate_prompt)
        async with self.http.post(url + "/inference", data=form, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status != 200:
                raise DictationError("transcription failed", 502)
            data = await r.json(content_type=None)
        if not isinstance(data, dict) or "error" in data:
            raise DictationError("transcription failed", 502)
        return clean(str(data.get("text", "")))

    async def transcribe(self, pcm: bytes, partial: bool = False) -> str | None:
        """16 kHz mono s16le PCM -> text. A partial returns None instead of waiting its turn."""
        if is_silent(pcm):
            return ""
        url = await self.ensure()
        if partial and self.lock.locked():
            return None
        async with self.lock:
            try:
                return await self._post(url, pcm, partial)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("dictation: whisper-server request failed: %s", type(e).__name__)
                await self._kill()  # a fresh start next time
                raise DictationError("transcription failed", 502) from e
            finally:
                self._touch()

    async def transcribe_upload(self, audio: bytes) -> str:
        """A finished recording (any container ffmpeg reads) -> text."""
        if not audio:
            raise DictationError("no audio")
        if len(audio) > MAX_UPLOAD:
            raise DictationError("recording too long", 413)
        if not self.cfg.ffmpeg:
            raise DictationError("uploads need ffmpeg; this browser should stream", 501)
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = tempfile.mkdtemp(prefix="dictate-", dir=self.cfg.runtime_dir)
        try:
            src = os.path.join(tmp, "in")  # a file: mp4 cannot be read from a pipe
            fd = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(audio)
            argv = [self.cfg.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", src,
                    "-t", str(self.cfg.dictate_max_seconds), "-ar", str(RATE), "-ac", "1", "-f", "s16le", "-"]  # fmt: skip
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                pcm, err = await asyncio.wait_for(proc.communicate(), 20)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise DictationError("could not decode the recording", 422)
            if proc.returncode != 0:
                log.warning("ffmpeg failed (%d): %s", proc.returncode, err.decode(errors="replace")[-300:])
                raise DictationError("could not decode the recording", 422)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return await self.transcribe(pcm) or ""

    # -- streaming --------------------------------------------------------------

    async def stream(self, ws: web.WebSocketResponse) -> None:
        """One recording over a WebSocket.

        Client -> binary frames of 16 kHz mono s16le PCM, then {"t":"stop"} for the
        final text or {"t":"cancel"}. Server -> {"t":"ready"} once the model is warm,
        {"t":"partial","text"} while recording, {"t":"final","text"} or
        {"t":"error","message"}, then it closes.
        """
        self.streams += 1
        pcm = bytearray()
        limit = self.cfg.dictate_max_seconds * BYTES_PER_SECOND
        loop = asyncio.get_running_loop()
        opened = loop.time()
        stats = {"first_audio": None, "partials": 0, "final_ms": None, "ready_after": None}

        async def send(msg: dict) -> None:
            if not ws.closed:
                try:
                    await ws.send_str(json.dumps(msg))
                except ConnectionError:
                    pass

        async def warm() -> None:
            try:
                await self.ensure()
                stats["ready_after"] = loop.time() - opened
                await send({"t": "ready"})
            except DictationError as e:
                await send({"t": "error", "message": str(e)})

        async def partials() -> None:
            done, last = 0, ""
            while True:
                await more.wait()
                more.clear()
                if not self.ready or len(pcm) - done < PARTIAL_MIN_NEW:
                    continue
                n = len(pcm)
                try:
                    text = await self.transcribe(bytes(pcm[:n]), partial=True)
                except DictationError:
                    continue
                if text is None:
                    continue
                done = n
                if text != last:
                    last = text
                    stats["partials"] += 1
                    await send({"t": "partial", "text": text})

        more = asyncio.Event()  # set on new audio and when the model is ready
        warming = asyncio.create_task(warm())
        warming.add_done_callback(lambda _: more.set())
        partial_task = asyncio.create_task(partials())
        final = False
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if stats["first_audio"] is None:
                        stats["first_audio"] = loop.time() - opened
                    pcm += msg.data[: max(0, limit - len(pcm))]
                    more.set()
                    if len(pcm) >= limit:
                        final = True  # at the cap: transcribe what we have
                        break
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        kind = json.loads(msg.data).get("t")
                    except (ValueError, AttributeError):
                        kind = None
                    if kind in ("stop", "cancel"):
                        final = kind == "stop"
                        break
                else:
                    break
            partial_task.cancel()
            if final:
                started = loop.time()
                try:
                    text = await self.transcribe(bytes(pcm)) or ""
                    stats["final_ms"] = round((loop.time() - started) * 1000)
                    await send({"t": "final", "text": text, "limit": len(pcm) >= limit})
                except DictationError as e:
                    await send({"t": "error", "message": str(e)})
        finally:
            partial_task.cancel()
            warming.cancel()
            self.streams -= 1
            if not ws.closed:
                await ws.close()
            # Timings only, never audio or text: enough to see where a slow one went.
            log.info(
                "dictation: %s, %.1fs audio over %.1fs, first audio after %s, ready after %s, %d partials, final %s",
                "done" if final else "cancelled",
                len(pcm) / BYTES_PER_SECOND,
                loop.time() - opened,
                _secs(stats["first_audio"]),
                _secs(stats["ready_after"]),
                stats["partials"],
                f"{stats['final_ms']}ms" if stats["final_ms"] is not None else "-",
            )


def _secs(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}s"
