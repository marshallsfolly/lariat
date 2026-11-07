"""tmux provider: list, attach, create, rename and kill sessions.

Polled every few seconds (tmux has no event stream worth the trouble), plus
an immediate refresh after each action. No server running = empty list.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from .. import validate
from ..config import Config
from . import ActionError

log = logging.getLogger("lariat.tmux")

FORMAT = "#{session_name}\t#{session_attached}\t#{session_activity}\t#{pane_current_path}\t#{session_windows}"


async def run(argv: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "timeout"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


class TmuxProvider:
    name = "tmux"
    POLL = 5.0

    def __init__(self, cfg: Config, hub):
        self.cfg = cfg
        self.hub = hub
        self.tmux = cfg.tmux
        self.sessions: list[dict] = []
        self.prefix = "\x02"
        self._seen: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._kick = asyncio.Event()

    def state(self) -> dict:
        items = []
        for s in self.sessions:
            items.append({**s, "unseen": s["activity"] > self._seen.get(s["name"], s["activity"])})
        return {
            "sessions": items,
            "default": self.cfg.tmux_default,
            "prefix": self.prefix,
        }

    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._kick.wait(), self.POLL)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()
            try:
                await self.refresh()
            except Exception:
                log.exception("tmux refresh failed")

    async def refresh(self) -> None:
        code, out, _ = await run([self.tmux, "list-sessions", "-F", FORMAT])
        sessions = []
        if code == 0:
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) != 5:
                    continue
                name, attached, activity, path, windows = parts
                sessions.append(
                    {
                        "name": name,
                        "attached": int(attached or 0),
                        "activity": int(activity or 0),
                        "cwd": os.path.basename(path.rstrip("/")) or path,
                        "windows": int(windows or 0),
                    }
                )
                self._seen.setdefault(name, int(activity or 0))
            prefix_code, prefix_out, _ = await run([self.tmux, "show-options", "-gv", "prefix"])
            if prefix_code == 0 and prefix_out.strip():
                self.prefix = validate.prefix_bytes(prefix_out.strip())
        sessions.sort(key=lambda s: s["name"])
        if sessions != self.sessions:
            self.sessions = sessions
        self.hub.changed()

    def mark_seen(self, name: str) -> None:
        self._seen[name] = int(time.time())
        self.hub.changed()

    def exists(self, name: str) -> bool:
        return any(s["name"] == name for s in self.sessions)

    def argv(self, target: str) -> list[str]:
        name = validate.tmux_name(target)
        # -A attaches when it exists; creating on attach is what the quick button wants.
        return [self.tmux, "new-session", "-A", "-s", name]

    async def preview(self, target: str, lines: int = 40) -> str:
        name = validate.tmux_name(target)
        if not self.exists(name):
            raise ActionError("no such tmux session", 404)
        code, out, err = await run([self.tmux, "capture-pane", "-p", "-J", "-t", f"={name}:", "-S", f"-{lines}"])
        if code != 0:
            raise ActionError(err.strip() or "capture failed", 502)
        return out.rstrip("\n")

    async def action(self, op: str, body: dict) -> dict:
        if op == "tmux_new":
            name = validate.tmux_name(body.get("name"))
            if self.exists(name):
                raise ActionError("a session with that name exists")
            cwd = validate.cwd_under(body.get("cwd") or str(self.cfg.project_roots[0]), self.cfg.project_roots)
            code, _, err = await run([self.tmux, "new-session", "-d", "-s", name, "-c", str(cwd)])
        elif op == "tmux_rename":
            name = validate.tmux_name(body.get("target"))
            new = validate.tmux_name(body.get("name"))
            if not self.exists(name):
                raise ActionError("no such tmux session", 404)
            code, _, err = await run([self.tmux, "rename-session", "-t", f"={name}", new])
        elif op == "tmux_kill":
            name = validate.tmux_name(body.get("target"))
            if not self.exists(name):
                raise ActionError("no such tmux session", 404)
            code, _, err = await run([self.tmux, "kill-session", "-t", f"={name}"])
        else:
            raise ActionError("unknown action", 400)
        if code != 0:
            raise ActionError(err.strip() or "tmux failed", 502)
        self._kick.set()
        return {"ok": True}
