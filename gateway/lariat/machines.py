"""The hub's other machines: the phone talks to one lariat, which reaches the rest.

machines.json (0600) lists them: a name, the machine's URL, the link token it
gave us (`lariat link` there) and, if it uses its own CA, that CA, which we
pin. For each, the hub follows its live state and alerts (its /api/events),
puts them in the hub's own state under `machines`, and forwards the alerts to
the phone (inbox and push, labelled with the machine). The gateway proxies
actions, previews, uploads and terminals to it (app.py, /m/<name>/...).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import ssl
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp

from . import tls

log = logging.getLogger("lariat.machines")

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
RELOAD_EVERY = 3.0


# Join codes ("Add a machine" on the phone): a one-time token, plus a short
# fingerprint of the hub's certificate when that is not publicly trusted.
ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no 0/O, 1/I/L, U
TOKEN_LEN = 12
PIN_LEN = 8
INVITE_TTL = 15 * 60


def _encode(data: bytes, length: int) -> str:
    n = int.from_bytes(data, "big")
    out = []
    for _ in range(length):
        n, r = divmod(n, len(ALPHABET))
        out.append(ALPHABET[r])
    return "".join(out)


def pin_of_der(der: bytes) -> str:
    return _encode(hashlib.sha256(der).digest(), PIN_LEN)


def pin_of_file(path: Path) -> str:
    """The fingerprint of the first certificate in a PEM file (the one served)."""
    pem = path.read_text()
    first = pem[: pem.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")]
    return pin_of_der(ssl.PEM_cert_to_DER_cert(first))


def normalize_code(code: str) -> str:
    return re.sub(r"[\s-]", "", code).upper()


def format_code(code: str) -> str:
    return "-".join(code[i : i + 4] for i in range(0, len(code), 4))


def loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Invites:
    """Open join codes, in memory: a restart cancels them."""

    def __init__(self, clock=time.monotonic):
        self._open: dict[str, float] = {}  # sha256(token) -> expiry
        self._clock = clock

    def _prune(self) -> None:
        now = self._clock()
        for k in [k for k, exp in self._open.items() if exp <= now]:
            del self._open[k]

    def create(self) -> str:
        self._prune()
        while len(self._open) >= 5:  # a handful at most
            del self._open[min(self._open, key=self._open.get)]
        token = "".join(secrets.choice(ALPHABET) for _ in range(TOKEN_LEN))
        self._open[hashlib.sha256(token.encode()).hexdigest()] = self._clock() + INVITE_TTL
        return token

    def _find(self, token: str) -> str | None:
        self._prune()
        digest = hashlib.sha256(token.encode()).hexdigest()
        return next((k for k in self._open if hmac.compare_digest(k, digest)), None)

    def valid(self, token: str) -> bool:
        return self._find(token) is not None

    def take(self, token: str) -> bool:
        key = self._find(token)
        if key is None:
            return False
        del self._open[key]
        return True


def check_name(name: str) -> str:
    name = name.strip().lower()
    if not NAME.match(name):
        raise ValueError("machine names are 1-32 lowercase letters, digits and dashes (pass --name)")
    return name


def ssl_context(ca_pem: str | None) -> ssl.SSLContext:
    if not ca_pem:
        return tls.public_context()
    # The machine's CA (or its own certificate), pinned: only it is trusted.
    ctx = ssl.create_default_context(cadata=ca_pem)
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN  # a pinned certificate need not be self-signed
    return ctx


def probe(url: str, token: str, ca_pem: str | None) -> str:
    """Check the machine answers with this token; returns its app name."""
    req = urllib.request.Request(url + "/api/state", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=ssl_context(ca_pem)) as r:
            return json.load(r).get("config", {}).get("appName", "lariat")
    except urllib.error.HTTPError as e:
        raise ValueError(f"{url} refused the link ({e.code}): make a new code with `lariat link` there") from e
    except (urllib.error.URLError, OSError) as e:
        raise ValueError(f"cannot reach {url} ({getattr(e, 'reason', e)}): is lariat running there, and Tailscale on here?") from e


class Registry:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return []
        return [d for d in data if isinstance(d, dict) and {"name", "url", "token"} <= d.keys()]

    def _save(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(entries, f, indent=1)
        os.replace(tmp, self.path)

    def add(self, name: str, url: str, token: str, ca: str | None) -> None:
        entries = [e for e in self.load() if e["name"] != name]
        entries.append({"name": name, "url": url.rstrip("/"), "token": token, "ca": ca})
        self._save(entries)

    def remove(self, name: str) -> bool:
        entries = self.load()
        kept = [e for e in entries if e["name"] != name]
        if len(kept) == len(entries):
            return False
        self._save(kept)
        return True


def own_state(state) -> dict | None:
    """A machine's state without the machines it is itself the hub for (two
    machines may each be the other's hub; their states must not nest)."""
    if not isinstance(state, dict):
        return None
    return {k: v for k, v in state.items() if k != "machines"}


def forwarded_alert(machine: str, item: dict) -> dict:
    """A machine's alert as the hub shows and pushes it."""
    out = dict(item)
    out["id"] = f"{machine}:{item.get('id', '')}"
    out["machine"] = machine
    title = item.get("pushTitle") or item.get("title") or "lariat"
    out["pushTitle"] = f"{machine}: {title}"
    url = str(item.get("url") or "")
    out["url"] = url.replace("/#go=", f"/#go=m:{machine}:", 1) if url.startswith("/#go=") else "/"
    return out


class Remote:
    """One machine: follows its state and alerts over its event stream."""

    def __init__(self, owner: "Machines", entry: dict):
        self.owner = owner
        self.entry = entry
        self.name = entry["name"]
        self.connected = False
        self.config: dict | None = None
        self.state: dict | None = None
        self.ssl = ssl_context(entry.get("ca"))
        self._task = asyncio.create_task(self._run(), name=f"machine:{self.name}")

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.entry['token']}"}

    def public(self) -> dict:
        return {"name": self.name, "connected": self.connected, "config": self.config, "state": self.state}

    def stop(self) -> None:
        self._task.cancel()

    def _set(self, connected: bool) -> None:
        if connected != self.connected:
            self.connected = connected
            log.info("machine %s %s", self.name, "connected" if connected else "unreachable")
        self.owner.hub.changed()

    async def _run(self) -> None:
        backoff = 1.0
        http = self.owner.http
        url = self.entry["url"]
        while True:
            try:
                async with http.get(url + "/api/state", headers=self.headers, ssl=self.ssl, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    r.raise_for_status()
                    data = await r.json(content_type=None)
                self.config, self.state = data.get("config"), own_state(data.get("state"))
                self._set(True)
                timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=60)  # it pings every 20 s
                async with http.get(url + "/api/events", headers=self.headers, ssl=self.ssl, timeout=timeout) as r:
                    r.raise_for_status()
                    backoff = 1.0
                    event, data_lines = "message", []
                    async for raw in r.content:
                        line = raw.decode(errors="replace").rstrip("\r\n")
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].strip())
                        elif not line:
                            if data_lines:
                                self._dispatch(event, "\n".join(data_lines))
                            event, data_lines = "message", []
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
                log.debug("machine %s: %s", self.name, e)
            self._set(False)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _dispatch(self, event: str, data: str) -> None:
        try:
            payload = json.loads(data)
        except ValueError:
            return
        if event == "state":
            self.state = own_state(payload)
            self.owner.hub.changed()
        elif event == "alert" and isinstance(payload, dict) and not payload.get("machine"):
            # Only its own alerts: one it forwarded from elsewhere reached the phone already (or loops).
            self.owner.on_alert(forwarded_alert(self.name, payload))


class Machines:
    name = "machines"

    def __init__(self, path: Path, hub, on_alert):
        self.registry = Registry(path)
        self.hub = hub
        self.on_alert = on_alert  # (item) -> None: the hub's inbox and push
        self.remotes: dict[str, Remote] = {}
        self.http: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None

    def state(self) -> list[dict]:
        return [r.public() for r in self.remotes.values()]

    def get(self, name: str) -> Remote | None:
        return self.remotes.get(name)

    def sync(self) -> None:
        """Follow machines.json: start new machines, stop removed or changed ones."""
        wanted = {e["name"]: e for e in self.registry.load()}
        for name in list(self.remotes):
            if name not in wanted or wanted[name] != self.remotes[name].entry:
                self.remotes.pop(name).stop()
                self.hub.changed()
        for name, entry in wanted.items():
            if name not in self.remotes:
                self.remotes[name] = Remote(self, entry)
                self.hub.changed()

    async def check(self, url: str, token: str, ca: str | None) -> None:
        """Does the machine answer with this token? (Before adding it.)"""
        try:
            async with self.http.get(
                url + "/api/state", headers={"Authorization": f"Bearer {token}"}, ssl=ssl_context(ca), timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    raise ValueError(f"{url} refused its own link ({r.status})")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise ValueError(f"the hub cannot reach {url}: is lariat running there, as a service?") from e

    async def unlink(self, name: str) -> bool:
        """Ask a machine to revoke our link (best effort)."""
        entry = next((e for e in self.registry.load() if e["name"] == name), None)
        if entry is None:
            return False
        try:
            async with self.http.post(
                entry["url"] + "/api/unlink",
                headers={"Authorization": f"Bearer {entry['token']}"},
                ssl=ssl_context(entry.get("ca")),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                return r.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False

    async def start(self) -> None:
        self.http = aiohttp.ClientSession()

        async def loop() -> None:
            while True:
                self.sync()
                await asyncio.sleep(RELOAD_EVERY)

        self._task = asyncio.create_task(loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for remote in self.remotes.values():
            remote.stop()
        if self.http:
            await self.http.close()
