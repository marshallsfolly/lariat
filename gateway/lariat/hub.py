"""Fan-out of dashboard state and alerts to every open SSE stream.

Providers call `changed()` whenever their state may differ; the hub
debounces, rebuilds the full normalized state (it is small) and pushes it
only if it actually changed. Clients treat each `state` event as the whole
truth, so a dropped event never leaves them inconsistent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

log = logging.getLogger("lariat.hub")


class Hub:
    def __init__(self, debounce: float = 0.05):
        self.debounce = debounce
        self._subscribers: set[asyncio.Queue] = set()
        self.alert_listeners: list[Callable[[dict], None]] = []
        self._sources: dict[str, Callable[[], Any]] = {}
        self._pending: asyncio.TimerHandle | None = None
        self._last: str = ""
        self.version = 0

    def register(self, name: str, source: Callable[[], Any]) -> None:
        self._sources[name] = source

    def state(self) -> dict:
        out = {}
        for name, source in self._sources.items():
            try:
                out[name] = source()
            except Exception:  # a broken provider must not take the dashboard down
                log.exception("provider %s failed to report state", name)
                out[name] = {"error": True}
        return out

    def changed(self) -> None:
        if self._pending is not None:
            return
        loop = asyncio.get_running_loop()
        self._pending = loop.call_later(self.debounce, self._flush)

    def _flush(self) -> None:
        self._pending = None
        state = self.state()
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":"))
        if encoded == self._last:
            return
        self._last = encoded
        self.version += 1
        self._broadcast("state", encoded)

    def alert(self, item: dict) -> None:
        self._broadcast("alert", json.dumps(item, separators=(",", ":")))
        for listener in self.alert_listeners:
            try:
                listener(item)
            except Exception:
                log.exception("alert listener failed")

    def _broadcast(self, event: str, data: str) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                # A slow client only needs the newest state; drop what it hasn't read.
                while not queue.empty():
                    queue.get_nowait()
            queue.put_nowait((event, data))

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
