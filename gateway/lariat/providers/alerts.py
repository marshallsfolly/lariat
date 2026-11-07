"""Alerts provider: the inbox. Fed by `lariat notify` (agent hooks on this
machine) and, when configured, an ntfy topic on loopback.

Streams `<url>/<topic>/json` and keeps the last 50 messages. Titles in the
form "<agent> - <project> (<host>)" are split so the dashboard can match an
alert to the agent pane working in that project. herdr's own status stays
authoritative; this is history and context.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque

import aiohttp

from ..config import Config

log = logging.getLogger("lariat.alerts")

TITLE = re.compile(r"^(?P<agent>.+?) - (?P<project>.+?)(?: \((?P<host>[^)]*)\))?$")
KEEP = 50


def parse(msg: dict) -> dict | None:
    if msg.get("event") != "message":
        return None
    title = str(msg.get("title") or "")
    m = TITLE.match(title)
    tags = [t for t in msg.get("tags") or [] if isinstance(t, str)]
    kind = "blocked" if "bell" in tags else "done" if "white_check_mark" in tags else "info"
    return {
        "id": str(msg.get("id") or ""),
        "time": int(msg.get("time") or 0),
        "title": title[:200],
        "message": str(msg.get("message") or "")[:1000],
        "agent": m.group("agent") if m else None,
        "project": m.group("project") if m else None,
        "kind": kind,
    }


class AlertsProvider:
    name = "alerts"

    def __init__(self, cfg: Config, hub):
        self.cfg = cfg
        self.hub = hub
        self.items: deque[dict] = deque(maxlen=KEEP)
        self.connected = False
        self._last_id: str | None = None
        self._task: asyncio.Task | None = None

    def state(self) -> dict:
        return {"ntfy": bool(self.cfg.ntfy_url), "connected": self.connected, "items": list(reversed(self.items))}

    def add(self, item: dict) -> None:
        """An alert from this machine (`lariat notify`)."""
        self.items.append(item)
        self.hub.alert(item)
        self.hub.changed()

    async def start(self) -> None:
        if self.cfg.ntfy_url:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        url = f"{self.cfg.ntfy_url}/{self.cfg.ntfy_topic}/json"
        backoff = 1.0
        timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_read=90)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            while True:
                since = self._last_id or "1h"
                try:
                    async with http.get(url, params={"since": since}) as resp:
                        resp.raise_for_status()
                        self.connected = True
                        self.hub.changed()
                        backoff = 1.0
                        async for raw in resp.content:
                            self._handle(raw)
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    log.debug("ntfy stream: %s", e)
                if self.connected:
                    self.connected = False
                    self.hub.changed()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _handle(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        item = parse(msg)
        if item is None:
            return
        if any(i["id"] == item["id"] for i in self.items):
            return
        self._last_id = item["id"]
        self.items.append(item)
        self.hub.alert(item)
        self.hub.changed()
