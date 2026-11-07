"""herdr provider: live workspaces/tabs/panes/agents over herdr's JSON socket API.

One supervisor task per herdr session keeps an `events.subscribe` connection
open and re-reads `session.snapshot` whenever an event arrives (events are
invalidation signals only). Agent status changes are only delivered for
panes named in the subscription, so the subscription is renewed whenever the
pane set changes. A slow safety poll covers anything missed.

Only the fixed operations below are ever sent; client JSON is never forwarded.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from .. import validate
from ..config import Config
from . import ActionError

log = logging.getLogger("lariat.herdr")

EVENT_TYPES = [
    "workspace.created",
    "workspace.updated",
    "workspace.renamed",
    "workspace.closed",
    "workspace.moved",
    "workspace.reordered",
    "tab.created",
    "tab.closed",
    "tab.renamed",
    "tab.moved",
    "pane.created",
    "pane.closed",
    "pane.updated",
    "pane.moved",
    "pane.exited",
    "pane.agent_detected",
]

# Every herdr method lariat may call. Anything else is unreachable from the API.
METHODS = {
    "session.snapshot",
    "events.subscribe",
    "pane.read",
    "agent.focus",
    "agent.rename",
    "agent.send_keys",
    "agent.prompt",
    "agent.start",
    "workspace.create",
    "workspace.rename",
    "workspace.close",
    "tab.rename",
    "pane.close",
    "pane.focus",
}

STATUSES = ("working", "blocked", "done", "idle", "unknown")


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


async def call(sock: str, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(sock, limit=1 << 25), 3.0)
    try:
        writer.write(json.dumps({"id": "lariat", "method": method, "params": params or {}}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
    finally:
        writer.close()
    if not line:
        raise HerdrError("closed", "herdr closed the connection")
    msg = json.loads(line)
    if "error" in msg:
        err = msg["error"] or {}
        raise HerdrError(str(err.get("code", "error")), str(err.get("message", "")))
    return msg.get("result") or {}


def normalize(snap: dict) -> dict:
    agents = {a.get("pane_id"): a for a in snap.get("agents") or []}
    tabs_by_ws: dict[str, list] = {}
    for tab in snap.get("tabs") or []:
        tabs_by_ws.setdefault(tab.get("workspace_id"), []).append(tab)
    panes_by_tab: dict[str, list] = {}
    for pane in snap.get("panes") or []:
        panes_by_tab.setdefault(pane.get("tab_id"), []).append(pane)

    workspaces = []
    for ws in sorted(snap.get("workspaces") or [], key=lambda w: w.get("number", 0)):
        tabs = []
        for tab in sorted(tabs_by_ws.get(ws.get("workspace_id"), []), key=lambda t: t.get("number", 0)):
            panes = []
            for p in panes_by_tab.get(tab.get("tab_id"), []):
                a = agents.get(p.get("pane_id"), {})
                agent = p.get("agent") or a.get("agent")
                status = (a.get("agent_status") or p.get("agent_status")) if agent else None
                cwd = p.get("foreground_cwd") or p.get("cwd") or ""
                panes.append(
                    {
                        "id": p.get("pane_id"),
                        "agent": agent,
                        "display": p.get("display_agent") or a.get("display_agent") or agent,
                        "name": a.get("name"),
                        "label": p.get("label"),
                        "status": status if status in STATUSES else ("unknown" if agent else None),
                        "title": a.get("terminal_title_stripped") or p.get("terminal_title_stripped") or p.get("title"),
                        "cwd": os.path.basename(cwd.rstrip("/")) or cwd,
                        "focused": bool(p.get("focused")),
                        "completion_seq": a.get("completion_seq"),
                        "state_change_seq": a.get("state_change_seq"),
                    }
                )
            tabs.append(
                {
                    "id": tab.get("tab_id"),
                    "label": tab.get("label"),
                    "number": tab.get("number"),
                    "focused": bool(tab.get("focused")),
                    "panes": panes,
                }
            )
        workspaces.append(
            {
                "id": ws.get("workspace_id"),
                "label": ws.get("label"),
                "number": ws.get("number"),
                "focused": bool(ws.get("focused")),
                "status": ws.get("agent_status"),
                "tabs": tabs,
            }
        )
    return {"workspaces": workspaces, "focused_pane": snap.get("focused_pane_id")}


def pane_index(state: dict) -> dict[str, dict]:
    out = {}
    for ws in state.get("workspaces", []):
        for tab in ws["tabs"]:
            for pane in tab["panes"]:
                out[pane["id"]] = {**pane, "workspace": ws["id"], "tab": tab["id"]}
    return out


def transitions(session: str, before: dict[str, dict], after: dict[str, dict]) -> list[dict]:
    """Alerts for agents that just became blocked or finished (not on a first snapshot)."""
    if not before:
        return []
    out = []
    for pane_id, pane in after.items():
        old = before.get(pane_id)
        status = pane.get("status")
        if old is None or not pane.get("agent") or status not in ("blocked", "done") or old.get("status") == status:
            continue
        name = pane.get("name") or pane.get("display") or pane.get("agent")
        project = pane.get("cwd") or None
        out.append(
            {
                "id": f"herdr:{session}:{pane_id}:{pane.get('state_change_seq') or int(time.time())}",
                "time": int(time.time()),
                "title": f"{name} - {project}" if project else str(name),
                "message": pane.get("title") or "",
                "agent": name,
                "project": project,
                "kind": status,
                "session": session,
                "pane": pane_id,
                "pushTitle": f"{name} needs you" if status == "blocked" else f"{name} is done",
                "pushBody": " · ".join(x for x in (project, pane.get("title")) if x)[:180],
                "url": f"/#go=agent:{session}:{pane_id}",
            }
        )
    return out


class HerdrSession:
    SAFETY_POLL = 10.0

    def __init__(self, provider: "HerdrProvider", name: str, sock: str):
        self.provider = provider
        self.name = name
        self.sock = sock
        self.running = False
        self.state: dict = {"workspaces": [], "focused_pane": None}
        self.panes: dict[str, dict] = {}
        self._subscribed: frozenset[str] = frozenset()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._pending: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._supervise(), name=f"herdr:{self.name}")

    def stop(self) -> None:
        for task in (self._task, self._pending):
            if task:
                task.cancel()

    async def call(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        self.provider.require_method(method)
        return await call(self.sock, method, params, timeout)

    async def refresh(self) -> None:
        async with self._lock:
            result = await self.call("session.snapshot")
            snap = result.get("snapshot") or {}
            self.state = normalize(snap)
            before, self.panes = self.panes, pane_index(self.state)
            self.running = True
        for item in transitions(self.name, before, self.panes):
            self.provider.hub.alert(item)
        self.provider.hub.changed()

    def _schedule_refresh(self) -> None:
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._delayed_refresh())

    async def _delayed_refresh(self) -> None:
        await asyncio.sleep(0.08)
        try:
            await self.refresh()
        except (OSError, HerdrError, asyncio.TimeoutError, ValueError) as e:
            log.debug("herdr %s refresh failed: %s", self.name, e)

    async def _supervise(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self.refresh()
                await self._listen()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except (OSError, HerdrError, asyncio.TimeoutError, ValueError, asyncio.IncompleteReadError) as e:
                if self.running:
                    log.info("herdr session %s unavailable: %s", self.name, e)
                self.running = False
                self.provider.hub.changed()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    async def _listen(self) -> None:
        """Hold one subscription until the pane set changes or the socket drops."""
        panes = frozenset(self.panes)
        subs = [{"type": t} for t in EVENT_TYPES]
        subs += [{"type": "pane.agent_status_changed", "pane_id": p} for p in sorted(panes)]
        self.provider.require_method("events.subscribe")
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.sock, limit=1 << 25), 3.0)
        try:
            writer.write(
                json.dumps({"id": "sub", "method": "events.subscribe", "params": {"subscriptions": subs}}).encode()
                + b"\n"
            )
            await writer.drain()
            first = json.loads(await asyncio.wait_for(reader.readline(), 10.0) or b"{}")
            if "error" in first:
                err = first["error"] or {}
                raise HerdrError(str(err.get("code")), str(err.get("message")))
            self._subscribed = panes
            idle = 0.0
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), 2.0)
                except asyncio.TimeoutError:
                    idle += 2.0
                    if idle >= self.SAFETY_POLL:
                        idle = 0.0
                        await self.refresh()
                else:
                    if not line:
                        raise HerdrError("closed", "event stream ended")
                    # Any event (including events_lost) just means: re-read the snapshot.
                    self._schedule_refresh()
                if frozenset(self.panes) != self._subscribed:
                    return
        finally:
            writer.close()

    async def preview(self, pane_id: str, lines: int = 40) -> str:
        if pane_id not in self.panes:
            raise ActionError("no such pane", 404)
        result = await self.call(
            "pane.read",
            {"pane_id": pane_id, "source": "recent_unwrapped", "lines": lines, "format": "text", "strip_ansi": True},
        )
        return ((result.get("read") or {}).get("text") or "").rstrip("\n")

    def public(self, default: bool) -> dict:
        counts = {s: 0 for s in STATUSES}
        for pane in self.panes.values():
            if pane["status"]:
                counts[pane["status"]] += 1
        return {"name": self.name, "default": default, "running": self.running, "counts": counts, **self.state}


class HerdrProvider:
    name = "herdr"
    DISCOVERY = 30.0

    def __init__(self, cfg: Config, hub):
        self.cfg = cfg
        self.hub = hub
        self.herdr = cfg.herdr
        self.sessions: dict[str, HerdrSession] = {}
        self.default_name = "default"
        self.available = self._load_methods(cfg.herdr_methods_file)
        self._task: asyncio.Task | None = None

    @staticmethod
    def _load_methods(path: Path | None) -> set[str]:
        if path is None:
            return set(METHODS)
        try:
            schema_methods = set(json.loads(path.read_text()))
        except (OSError, ValueError):
            log.warning("could not read herdr method list %s; assuming all methods exist", path)
            return set(METHODS)
        missing = METHODS - schema_methods
        if missing:
            log.warning("this herdr lacks %s; those actions are disabled", ", ".join(sorted(missing)))
        return METHODS & schema_methods

    def require_method(self, method: str) -> None:
        if method not in METHODS:
            raise ActionError("method not allowed", 403)
        if method not in self.available:
            raise ActionError(f"this herdr version has no {method}", 501)

    def _default_socket(self, name: str) -> str:
        base = Path.home() / ".config/herdr"
        if name == "default":
            return str(base / "herdr.sock")
        return str(base / "sessions" / name / "herdr.sock")

    async def _discover(self) -> dict[str, tuple[str, bool]]:
        found: dict[str, tuple[str, bool]] = {}
        try:
            proc = await asyncio.create_subprocess_exec(
                self.herdr,
                "session",
                "list",
                "--json",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 10.0)
            for s in json.loads(out or b"{}").get("sessions", []):
                name = s.get("name")
                if not isinstance(name, str) or not validate.HERDR_SESSION.match(name):
                    continue
                if s.get("default"):
                    self.default_name = name
                if s.get("running"):
                    found[name] = (s.get("socket_path") or self._default_socket(name), True)
        except (OSError, ValueError, asyncio.TimeoutError) as e:
            log.debug("herdr session discovery failed: %s", e)
        for name in self.cfg.herdr_sessions:
            if validate.HERDR_SESSION.match(name) and name not in found:
                found[name] = (self._default_socket(name), False)
        return found

    async def _sync_sessions(self) -> None:
        found = await self._discover()
        for name in list(self.sessions):
            if name not in found:
                self.sessions.pop(name).stop()
        for name, (sock, _running) in found.items():
            session = self.sessions.get(name)
            if session is None or session.sock != sock:
                if session:
                    session.stop()
                session = HerdrSession(self, name, sock)
                self.sessions[name] = session
            session.start()
        self.hub.changed()

    async def start(self) -> None:
        await self._sync_sessions()
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.DISCOVERY)
            try:
                await self._sync_sessions()
            except Exception:
                log.exception("herdr session sync failed")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for session in self.sessions.values():
            session.stop()

    def waiting(self) -> int:
        """Agents that need you, across sessions (the app icon badge)."""
        return sum(1 for sess in self.sessions.values() for p in sess.panes.values() if p.get("status") == "blocked")

    def state(self) -> dict:
        names = sorted(self.sessions, key=lambda n: (n != self.default_name, n))
        return {
            "sessions": [self.sessions[n].public(n == self.default_name) for n in names],
            "agentKinds": self.cfg.agent_kinds,
            "prefix": validate.prefix_bytes(self.cfg.herdr_prefix),
        }

    def session(self, name) -> HerdrSession:
        name = validate.herdr_session(name)
        if name == "default":
            name = self.default_name
        session = self.sessions.get(name)
        if session is None:
            raise ActionError("no such herdr session", 404)
        return session

    def _session_args(self, session: HerdrSession) -> list[str]:
        return [] if session.name == self.default_name else ["--session", session.name]

    def argv_agent(self, session_name, target) -> list[str]:
        session = self.session(session_name)
        pane_id = validate.herdr_id(target)
        pane = session.panes.get(pane_id)
        if pane is None or not pane["agent"]:
            raise ActionError("no agent in that pane", 404)
        return [self.herdr, *self._session_args(session), "agent", "attach", pane_id]

    def argv_tui(self, session_name) -> list[str]:
        name = validate.herdr_session(session_name)
        if name == "default" or name == self.default_name:
            return [self.herdr]
        if name not in self.sessions:
            raise ActionError("no such herdr session", 404)
        return [self.herdr, "--session", name]

    async def preview(self, session_name, target, lines: int = 40) -> str:
        return await self.session(session_name).preview(validate.herdr_id(target), lines)

    async def action(self, op: str, body: dict) -> dict:
        session = self.session(body.get("session"))
        try:
            result = await self._action(session, op, body)
        except HerdrError as e:
            raise ActionError(e.message or e.code, 502) from e
        except (OSError, asyncio.TimeoutError) as e:
            raise ActionError("herdr is not reachable", 503) from e
        await session.refresh()
        return result

    def _pane(self, session: HerdrSession, value, agent: bool = False) -> str:
        pane_id = validate.herdr_id(value)
        pane = session.panes.get(pane_id)
        if pane is None:
            raise ActionError("no such pane", 404)
        if agent and not pane["agent"]:
            raise ActionError("no agent in that pane")
        return pane_id

    def _workspace(self, session: HerdrSession, value) -> str:
        ws_id = validate.herdr_id(value)
        if not any(ws["id"] == ws_id for ws in session.state["workspaces"]):
            raise ActionError("no such workspace", 404)
        return ws_id

    def _tab(self, session: HerdrSession, value) -> str:
        tab_id = validate.herdr_id(value)
        if not any(tab["id"] == tab_id for ws in session.state["workspaces"] for tab in ws["tabs"]):
            raise ActionError("no such tab", 404)
        return tab_id

    async def _action(self, session: HerdrSession, op: str, body: dict) -> dict:
        if op == "focus":
            await session.call("agent.focus", {"target": self._pane(session, body.get("target"), agent=True)})
        elif op == "focus_pane":
            await session.call("pane.focus", {"pane_id": self._pane(session, body.get("target"))})
        elif op == "rename_agent":
            target = self._pane(session, body.get("target"), agent=True)
            name = body.get("name")
            await session.call("agent.rename", {"target": target, "name": validate.agent_name(name) if name else None})
        elif op == "rename_workspace":
            await session.call(
                "workspace.rename",
                {"workspace_id": self._workspace(session, body.get("id")), "label": validate.label(body.get("name"))},
            )
        elif op == "rename_tab":
            await session.call(
                "tab.rename", {"tab_id": self._tab(session, body.get("id")), "label": validate.label(body.get("name"))}
            )
        elif op == "close_pane":
            await session.call("pane.close", {"pane_id": self._pane(session, body.get("target"))})
        elif op == "close_workspace":
            # Never close_group: that would also close linked worktree workspaces.
            await session.call("workspace.close", {"workspace_id": self._workspace(session, body.get("id"))})
        elif op == "keys":
            await session.call(
                "agent.send_keys",
                {"target": self._pane(session, body.get("target"), agent=True), "keys": validate.keys(body.get("keys"))},
            )
        elif op == "prompt":
            await session.call(
                "agent.prompt",
                {"target": self._pane(session, body.get("target"), agent=True), "text": validate.text(body.get("text"))},
                timeout=30.0,
            )
        elif op == "new_agent":
            return await self._new_agent(session, body)
        else:
            raise ActionError("unknown action")
        return {"ok": True}

    async def _new_agent(self, session: HerdrSession, body: dict) -> dict:
        kind = body.get("kind")
        if kind not in self.cfg.agent_kinds:
            raise ActionError("that agent kind is not offered here")
        cwd = validate.cwd_under(body.get("cwd"), self.cfg.project_roots)
        name = validate.agent_name(body.get("name"))
        created = await session.call(
            "workspace.create", {"cwd": str(cwd), "label": cwd.name or str(cwd), "focus": False}
        )
        pane_id = (created.get("root_pane") or {}).get("pane_id")
        if not pane_id:
            raise ActionError("herdr did not return the new pane", 502)
        last: HerdrError | None = None
        # The new pane's shell needs a moment to reach its prompt.
        for attempt in range(6):
            await asyncio.sleep(0.4 if attempt == 0 else 0.8)
            try:
                await session.call(
                    "agent.start",
                    {"name": name, "kind": kind, "pane_id": pane_id, "timeout_ms": 30000},
                    timeout=40.0,
                )
                return {"ok": True, "pane": pane_id}
            except HerdrError as e:
                last = e
                if e.code == "agent_not_ready":
                    # Started but blocked at a startup prompt; it shows as blocked on the dashboard.
                    return {"ok": True, "pane": pane_id, "warning": e.message}
                if e.code in ("unsupported_agent_kind", "invalid_request", "agent_name_taken", "name_taken"):
                    break
        raise ActionError(last.message if last else "agent did not start", 502)
