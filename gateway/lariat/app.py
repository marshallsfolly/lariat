"""The aiohttp application: static PWA, passkey auth, dashboard API, SSE, PTY bridge."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import time
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

from . import validate
from .auth import Auth, AuthError
from .config import Config, read_secret
from .dictate import MAX_STREAMS, Dictation, DictationError
from .hub import Hub
from .providers import ActionError
from .providers.alerts import AlertsProvider
from .providers.herdr import HerdrProvider
from .providers.shell import ShellProvider
from .providers.tmux import TmuxProvider
from .pty_bridge import CLOSE_EXPIRED, MAX_PTYS_PER_SESSION, Bridge, Pty, clamp_size
from .links import Links
from .machines import INVITE_TTL, TOKEN_LEN, Invites, Machines, check_name, format_code, loopback, normalize_code, pin_of_file
from .updates import Updater
from .push import Push, PushError
from .uploads import MAX_BYTES as UPLOAD_MAX, Uploads
from .sessions import Session, SessionStore

log = logging.getLogger("lariat")

# Styles allow 'unsafe-inline' only because xterm.js builds <style> elements at
# runtime; scripts, fonts and connections stay 'self'-only.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; worker-src 'self'; manifest-src 'self'; media-src 'self' blob:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), payment=(), usb=(), microphone=(self), "
    "clipboard-read=(self), clipboard-write=(self), publickey-credentials-get=(self), "
    "publickey-credentials-create=(self)",
}
CSRF_HEADER = "X-Lariat"
# Variables systemd sets for the service itself; shells should not inherit them.
SERVICE_ENV = {
    "INVOCATION_ID",
    "JOURNAL_STREAM",
    "NOTIFY_SOCKET",
    "LISTEN_PID",
    "LISTEN_FDS",
    "STATE_DIRECTORY",
    "RUNTIME_DIRECTORY",
    "MANAGERPID",
    "SYSTEMD_EXEC_PID",
}

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("image/svg+xml", ".svg")


class Static:
    """The PWA bundle, read into memory once (it is an immutable store path)."""

    def __init__(self, root: Path | None):
        self.files: dict[str, tuple[bytes, str, str]] = {}
        if root is None:
            return
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            data = path.read_bytes()
            ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/json", "application/manifest+json", "image/svg+xml"):
                ctype += "; charset=utf-8"
            etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
            self.files[rel] = (data, ctype, etag)

    def response(self, request: web.Request, rel: str) -> web.Response:
        entry = self.files.get(rel)
        if entry is None:
            raise web.HTTPNotFound()
        data, ctype, etag = entry
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        resp = web.Response(body=data if request.method != "HEAD" else None, headers=headers)
        resp.headers["Content-Type"] = ctype
        if request.method == "HEAD":
            resp.headers["Content-Length"] = str(len(data))
        return resp


def remote_addr(request: web.Request) -> str:
    """The client address: the proxy's X-Real-IP (nginx) or X-Forwarded-For (tailscale
    serve) when the peer is loopback; only a local proxy can set them."""
    peer = request.remote or ""
    try:
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        real = request.headers.get("X-Real-IP") or (request.headers.get("X-Forwarded-For") or "").split(",")[-1]
        if real.strip():
            return real.strip()
    return peer


def json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def cert_days_left(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except Exception:
        return None
    return (cert.not_valid_after_utc - datetime.now(timezone.utc)).days


class Gateway:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(cfg.state_dir, 0o700)
        key = read_secret(cfg.session_key_file)
        if len(key) < 16:
            raise SystemExit("lariat: the session key file is missing or shorter than 16 bytes")
        self.sessions = SessionStore(key, cfg.idle_minutes * 60, cfg.absolute_minutes * 60)
        self.auth = Auth(cfg)
        self.hub = Hub()
        self.static = Static(cfg.static_dir)
        self.cookie = "__Host-lariat" if cfg.secure_cookies else "lariat"
        self.pre_cookie = "__Host-lariat-pre" if cfg.secure_cookies else "lariat-pre"

        self.shell = ShellProvider(cfg) if cfg.shell_enable else None
        self.tmux = TmuxProvider(cfg, self.hub) if cfg.tmux else None
        self.herdr = HerdrProvider(cfg, self.hub) if cfg.herdr else None
        self.alerts = AlertsProvider(cfg, self.hub)  # the inbox: `lariat notify`, and ntfy if configured
        self.dictation = Dictation(cfg) if cfg.dictate else None
        self.uploads = Uploads(cfg.state_dir / "uploads", cfg.upload_days)
        self.links = Links(cfg.state_dir / "links.json")  # hubs allowed to reach this machine
        # Machines this one is the hub for: their state and alerts come here.
        self.machines = Machines(cfg.state_dir / "machines.json", self.hub, self.alerts.add)
        self.hub.register(self.machines.name, self.machines.state)
        self.invites = Invites()  # "Add a machine" codes
        # New releases: checked, installed when no terminal is open, then a restart.
        self.restart_requested = False
        self._main: asyncio.Task | None = None
        self.updates = Updater(cfg.state_dir, self.hub, busy=self.terminals_open, restart=self.request_restart)
        self.hub.register(self.updates.name, self.updates.state)
        for provider in (self.shell, self.tmux, self.herdr, self.alerts):
            if provider is not None:
                self.hub.register(provider.name, provider.state)
        self.push = (
            Push(cfg, badge=lambda: self.herdr.waiting() if self.herdr else None, valid=self.auth.known) if cfg.push else None
        )
        if self.push is not None:
            self.hub.alert_listeners.append(self.push.notify)

    # -- plumbing --------------------------------------------------------

    def client_config(self) -> dict:
        return {
            "appName": self.cfg.app_name,
            "providers": {
                "shell": self.shell is not None,
                "tmux": self.tmux is not None,
                "herdr": self.herdr is not None,
                "alerts": self.alerts is not None,
            },
            "dictation": self.dictation is not None,
            "push": {"key": self.push.vapid.public} if self.push else None,
            "projectRoots": [str(p) for p in self.cfg.project_roots],
            "home": str(Path.home()),
            "idleMinutes": self.cfg.idle_minutes,
        }

    def session(self, request: web.Request, touch: bool = True) -> Session | None:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):  # a hub that was given a link code (links.py)
            link = self.links.session(auth[7:].strip())
            if link is not None:
                request["lariat_link"] = True
            return link
        return self.sessions.get(request.cookies.get(self.cookie), touch=touch)

    def alive(self, session) -> bool:
        if getattr(session, "link", False):
            return self.links.active(session.name)  # revoking a link ends its terminals too
        return self.sessions.alive(session)

    def require(self, request: web.Request, touch: bool = True) -> Session:
        session = self.session(request, touch)
        if session is None:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "login required"}), content_type="application/json"
            )
        return session

    def check_origin(self, request: web.Request) -> None:
        if request.get("lariat_link"):
            return  # a bearer token is no ambient credential: nothing to forge across sites
        if request.headers.get("Origin") != self.cfg.origin:
            raise web.HTTPForbidden(text=json.dumps({"error": "bad origin"}), content_type="application/json")

    def check_csrf(self, request: web.Request) -> None:
        if request.get("lariat_link"):
            return  # see check_origin
        self.check_origin(request)
        if request.headers.get(CSRF_HEADER) != "1":
            raise web.HTTPForbidden(text=json.dumps({"error": "missing header"}), content_type="application/json")

    @staticmethod
    async def read_body(request: web.Request, limit: int) -> bytes:
        """The whole body, refusing more than `limit` bytes. (content.read(n)
        returns what has arrived so far: over a real network, a first chunk.)"""
        if request.content_length is not None and request.content_length > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=request.content_length)
        chunks, size = [], 0
        async for chunk in request.content.iter_any():
            size += len(chunk)
            if size > limit:
                raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=size)
            chunks.append(chunk)
        return b"".join(chunks)

    async def body_json(self, request: web.Request, limit: int = 64 * 1024) -> dict:
        raw = await self.read_body(request, limit)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise web.HTTPBadRequest(text=json.dumps({"error": "invalid JSON"}), content_type="application/json")
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text=json.dumps({"error": "expected an object"}), content_type="application/json")
        return data

    def set_session_cookie(self, resp: web.StreamResponse, value: str) -> None:
        resp.set_cookie(
            self.cookie,
            value,
            path="/",
            secure=self.cfg.secure_cookies,
            httponly=True,
            samesite="Strict",
            max_age=self.cfg.absolute_minutes * 60,
        )

    def set_pre_cookie(self, resp: web.StreamResponse, value: str) -> None:
        resp.set_cookie(
            self.pre_cookie,
            value,
            path="/",
            secure=self.cfg.secure_cookies,
            httponly=True,
            samesite="Strict",
            max_age=120,
        )

    # -- middleware ------------------------------------------------------

    @web.middleware
    async def headers_mw(self, request: web.Request, handler):
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
            if request.path.startswith(("/api/", "/auth/")) and not (e.text or "").startswith("{"):
                resp = json_error(e.status, e.reason)
        if not resp.prepared:
            for k, v in SECURITY_HEADERS.items():
                resp.headers.setdefault(k, v)
            if request.path.startswith(("/api/", "/auth/", "/ws/", "/m/", "/healthz")):
                resp.headers["Cache-Control"] = "no-store"
        if isinstance(resp, web.HTTPException):
            raise resp
        return resp

    # -- auth routes -----------------------------------------------------

    async def auth_info(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "enroll": self.auth.enrollment_open(),
                "appName": self.cfg.app_name,
                "loggedIn": self.session(request, touch=False) is not None,
            }
        )

    def _limit(self, request: web.Request) -> None:
        self.auth.limiter.check(remote_addr(request))

    async def login_options(self, request: web.Request) -> web.Response:
        self.check_csrf(request)
        try:
            self._limit(request)
            pre, options = self.auth.login_options()
        except AuthError as e:
            return json_error(e.status, str(e))
        resp = web.Response(text=options, content_type="application/json")
        self.set_pre_cookie(resp, pre)
        return resp

    async def login_verify(self, request: web.Request) -> web.Response:
        self.check_csrf(request)
        body = await self.body_json(request)
        try:
            self._limit(request)
            cred_id = self.auth.login_verify(request.cookies.get(self.pre_cookie), body.get("credential"))
        except AuthError as e:
            self.auth.limiter.failure()
            log.warning("login failed from %s: %s", remote_addr(request), e)
            return json_error(e.status, str(e))
        _, cookie = self.sessions.create(cred_id)
        log.info("login ok from %s (passkey %s…)", remote_addr(request), cred_id[:8])
        resp = web.json_response({"ok": True})
        self.set_session_cookie(resp, cookie)
        resp.del_cookie(self.pre_cookie, path="/")
        return resp

    async def logout(self, request: web.Request) -> web.Response:
        self.check_csrf(request)
        session = self.session(request, touch=False)
        if session and not getattr(session, "link", False):
            self.sessions.revoke(session)
        resp = web.json_response({"ok": True})
        resp.del_cookie(self.cookie, path="/")
        return resp

    async def register_options(self, request: web.Request) -> web.Response:
        self.check_csrf(request)
        try:
            self._limit(request)
            pre, options = self.auth.register_options(request.headers.get("X-Enroll-Token"))
        except AuthError as e:
            if e.status in (401, 403):
                self.auth.limiter.failure()
            log.warning("enrollment refused from %s: %s", remote_addr(request), e)
            return json_error(e.status, str(e))
        resp = web.Response(text=options, content_type="application/json")
        self.set_pre_cookie(resp, pre)
        return resp

    async def register_verify(self, request: web.Request) -> web.Response:
        self.check_csrf(request)
        body = await self.body_json(request)
        try:
            self._limit(request)
            record = self.auth.register_verify(
                request.cookies.get(self.pre_cookie),
                request.headers.get("X-Enroll-Token"),
                body.get("credential"),
                str(body.get("label") or ""),
            )
        except AuthError as e:
            self.auth.limiter.failure()
            log.warning("enrollment failed from %s: %s", remote_addr(request), e)
            return json_error(e.status, str(e))
        _, cookie = self.sessions.create(record["id"])
        resp = web.json_response({"ok": True, "credential": record})
        self.set_session_cookie(resp, cookie)
        resp.del_cookie(self.pre_cookie, path="/")
        return resp

    # -- API -------------------------------------------------------------

    async def api_state(self, request: web.Request) -> web.Response:
        self.require(request)
        return web.json_response({"config": self.client_config(), "state": self.hub.state()})

    async def api_events(self, request: web.Request) -> web.StreamResponse:
        session = self.require(request)
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                **SECURITY_HEADERS,
            }
        )
        await resp.prepare(request)
        queue = self.hub.subscribe()
        try:
            state = json.dumps(self.hub.state(), separators=(",", ":"))
            await resp.write(f"retry: 3000\nevent: state\ndata: {state}\n\n".encode())
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), 20.0)
                except asyncio.TimeoutError:
                    if not self.alive(session):
                        await resp.write(b"event: expired\ndata: {}\n\n")
                        break
                    await resp.write(b": ping\n\n")
                    continue
                if not self.alive(session):
                    await resp.write(b"event: expired\ndata: {}\n\n")
                    break
                await resp.write(f"event: {event}\ndata: {data}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.hub.unsubscribe(queue)
        return resp

    async def api_projects(self, request: web.Request) -> web.Response:
        self.require(request)
        roots = []
        for root in self.cfg.project_roots:
            try:
                entries = sorted(
                    e.name for e in os.scandir(root) if e.is_dir(follow_symlinks=True) and not e.name.startswith(".")
                )
            except OSError:
                continue
            roots.append({"path": str(root), "dirs": entries[:300]})
        return web.json_response({"roots": roots})

    async def api_preview(self, request: web.Request) -> web.Response:
        self.require(request, touch=False)
        kind = request.query.get("k")
        target = request.query.get("t", "")
        lines = max(1, min(int(request.query.get("n", "40") or 40), 2000))  # 2000: Select text
        try:
            if kind == "agent" and self.herdr:
                text = await self.herdr.preview(request.query.get("s"), target, lines)
            elif kind == "tmux" and self.tmux:
                text = await self.tmux.preview(target, lines)
            else:
                return json_error(404, "nothing to preview")
        except (ActionError, validate.Invalid) as e:
            return json_error(getattr(e, "status", 400), str(e))
        except (OSError, asyncio.TimeoutError):
            return json_error(503, "not reachable")
        return web.json_response({"text": text})

    async def api_action(self, request: web.Request) -> web.Response:
        session = self.require(request)
        self.check_csrf(request)
        body = await self.body_json(request)
        op = body.get("op")
        if not isinstance(op, str):
            return json_error(400, "missing op")
        try:
            if op.startswith("tmux_") and self.tmux:
                if op == "tmux_seen":
                    self.tmux.mark_seen(validate.tmux_name(body.get("target")))
                    result = {"ok": True}
                else:
                    result = await self.tmux.action(op, body)
            elif self.herdr and op in (
                "focus",
                "focus_pane",
                "rename_agent",
                "rename_workspace",
                "rename_tab",
                "new_agent",
                "close_pane",
                "close_workspace",
                "keys",
                "prompt",
            ):
                result = await self.herdr.action(op, body)
            else:
                return json_error(400, "unknown action")
        except (ActionError, validate.Invalid) as e:
            return json_error(getattr(e, "status", 400), str(e))
        log.info("action %s (passkey %s…)", op, session.cred_id[:8])
        return web.json_response(result)

    # -- push ----------------------------------------------------------------

    async def push_body(self, request: web.Request) -> tuple[Session, dict, str]:
        session = self.require(request)
        self.check_csrf(request)
        if self.push is None:
            raise web.HTTPNotFound(text=json.dumps({"error": "push is off"}), content_type="application/json")
        try:
            body = await request.json()
            device = validate.device(body.get("device"))
        except (ValueError, AttributeError, validate.Invalid) as e:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(e) or "bad request"}), content_type="application/json")
        return session, body, device

    async def api_push_subscribe(self, request: web.Request) -> web.Response:
        session, body, device = await self.push_body(request)
        kinds = body.get("kinds") if isinstance(body.get("kinds"), list) else ["blocked", "done"]
        try:
            self.push.subscribe(body.get("subscription") or {}, device, session.cred_id, [str(k) for k in kinds])
        except PushError as e:
            return json_error(e.status, str(e))
        return web.json_response({"ok": True})

    async def api_push_unsubscribe(self, request: web.Request) -> web.Response:
        _, _, device = await self.push_body(request)
        self.push.unsubscribe(device)
        return web.json_response({"ok": True})

    async def api_push_test(self, request: web.Request) -> web.Response:
        _, _, device = await self.push_body(request)
        try:
            await self.push.test(device)
        except PushError as e:
            return json_error(e.status, str(e))
        return web.json_response({"ok": True})

    async def api_notify(self, request: web.Request) -> web.Response:
        """An alert from an agent hook on this machine (`lariat notify`).

        Not a browser endpoint: it needs the token from the state dir, which only
        this user can read."""
        expected = read_secret(self.cfg.notify_token_file)
        given = (request.headers.get("X-Lariat-Notify") or "").encode()
        if len(expected) < 16 or not hmac.compare_digest(given, expected):
            return json_error(403, "bad notify token")
        try:
            body = await request.json()
            kind = body.get("kind") if body.get("kind") in ("blocked", "done", "info") else "info"
            agent = validate.label(body["agent"], 64) if body.get("agent") else None
            project = validate.label(body["project"], 64) if body.get("project") else None
            title = str(body.get("title") or "")[:200]
            message = str(body.get("message") or "")[:1000]
        except (ValueError, AttributeError, validate.Invalid):
            return json_error(400, "bad request")
        name = agent or "agent"
        item = {
            "id": f"local:{time.time_ns()}",
            "time": int(time.time()),
            "title": title or (f"{name} - {project}" if project else name),
            "message": message,
            "agent": agent,
            "project": project,
            "kind": kind,
            "pushTitle": title or {"blocked": f"{name} needs you", "done": f"{name} is done"}.get(kind, name),
            "pushBody": " · ".join(x for x in (project, message) if x)[:180],
        }
        self.alerts.add(item)
        return web.json_response({"ok": True})

    async def api_presence(self, request: web.Request) -> web.Response:
        # No touch: an app left open in front must still time out.
        self.require(request, touch=False)
        self.check_csrf(request)
        try:
            body = await request.json()
            device = validate.device(body.get("device"))
        except (ValueError, AttributeError, validate.Invalid):
            return json_error(400, "bad request")
        if self.push is not None:
            self.push.seen(device, bool(body.get("visible")))
        return web.json_response({"ok": True})

    async def api_upload(self, request: web.Request) -> web.Response:
        """An image from the phone; answers with the path to give the agent."""
        self.require(request)
        self.check_csrf(request)
        try:
            data = await self.read_body(request, UPLOAD_MAX)
        except web.HTTPRequestEntityTooLarge:
            return json_error(413, "image too large")
        try:
            path = self.uploads.save(data)
        except ValueError:
            return json_error(415, "not an image")
        log.info("upload: %s (%d KB)", path.name, len(data) // 1024)
        return web.json_response({"path": str(path)})

    async def api_dictate(self, request: web.Request) -> web.Response:
        self.require(request)
        self.check_csrf(request)
        if self.dictation is None:
            return json_error(404, "dictation is off")
        ctype = (request.content_type or "").lower()
        if not (ctype.startswith("audio/") or ctype == "application/octet-stream"):
            return json_error(415, "send audio")
        try:
            audio = await self.read_body(request, 2 * 1024 * 1024)
        except web.HTTPRequestEntityTooLarge:
            return json_error(413, "recording too long")
        try:
            text = await self.dictation.transcribe_upload(audio)
        except DictationError as e:
            return json_error(e.status, str(e))
        return web.json_response({"text": text})

    # -- terminal --------------------------------------------------------

    def term_argv(self, kind: str | None, target: str, session_name: str | None) -> list[str]:
        if kind == "shell" and self.shell:
            return self.shell.argv()
        if kind == "tmux" and self.tmux:
            return self.tmux.argv(target or (self.cfg.tmux_default or ""))
        if kind == "agent" and self.herdr:
            return self.herdr.argv_agent(session_name, target)
        if kind == "herdr" and self.herdr:
            return self.herdr.argv_tui(session_name)
        raise ActionError("unknown terminal kind", 404)

    def term_env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("LARIAT_") or key in SERVICE_ENV:
                env.pop(key)
        env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": env.get("LANG") or "C.UTF-8"})
        return env

    async def ws_term(self, request: web.Request) -> web.StreamResponse:
        session = self.session(request)
        if session is None:
            return json_error(401, "login required")
        if request.headers.get("Origin") != self.cfg.origin and not request.get("lariat_link"):
            return json_error(403, "bad origin")
        q = request.query
        try:
            argv = self.term_argv(q.get("k"), q.get("t", ""), q.get("s"))
        except (ActionError, validate.Invalid) as e:
            return json_error(getattr(e, "status", 400), str(e))
        cap = 4 * MAX_PTYS_PER_SESSION if getattr(session, "link", False) else MAX_PTYS_PER_SESSION  # a hub serves several phones
        if session.ptys >= cap:
            return json_error(429, "too many open terminals")
        cols, rows = clamp_size(q.get("c"), q.get("r"))

        ws = web.WebSocketResponse(max_msg_size=64 * 1024, heartbeat=30.0, compress=False)
        await ws.prepare(request)
        session.ptys += 1
        if q.get("k") == "tmux" and self.tmux:
            self.tmux.mark_seen(q.get("t") or self.cfg.tmux_default or "")
        log.info("terminal open: %s (passkey %s…)", " ".join(argv[1:4]) or argv[0], session.cred_id[:8])
        pty = Pty(argv, self.term_env(), str(Path.home()), cols, rows)
        bridge = Bridge(ws, pty, alive=lambda: self.alive(session), touch=lambda: self.sessions.touch(session))
        try:
            await bridge.run()
        except OSError as e:
            log.warning("terminal failed: %s", e)
            if not ws.closed:
                await ws.close(code=1011, message=b"spawn failed")
        finally:
            session.ptys -= 1
            if q.get("k") == "tmux" and self.tmux:
                self.tmux.mark_seen(q.get("t") or self.cfg.tmux_default or "")
        return ws

    async def ws_dictate(self, request: web.Request) -> web.StreamResponse:
        session = self.session(request)
        if session is None:
            return json_error(401, "login required")
        if request.headers.get("Origin") != self.cfg.origin and not request.get("lariat_link"):
            return json_error(403, "bad origin")
        if self.dictation is None:
            return json_error(404, "dictation is off")
        if self.dictation.streams >= MAX_STREAMS:
            return json_error(429, "busy with other recordings")
        ws = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=30.0, compress=False)
        await ws.prepare(request)
        await self.dictation.stream(ws)
        return ws

    # -- updates ------------------------------------------------------------

    def terminals_open(self) -> bool:
        """A restart ends open terminals (tmux and herdr sessions live on)."""
        return any(s.ptys for s in self.sessions.all()) or any(s.ptys for s in self.links.sessions())

    def request_restart(self) -> None:
        """Exit with updates.RESTART_CODE once shut down; the service manager starts the new binary."""
        self.restart_requested = True
        if self._main is not None:
            self._main.cancel()

    async def api_update(self, request: web.Request) -> web.Response:
        """The update state; POST {op: check|apply|mode, mode}. For the phone (a
        session; a hub's link) and `lariat update` (the local notify token)."""
        local = read_secret(self.cfg.notify_token_file)
        given = (request.headers.get("X-Lariat-Notify") or "").encode()
        if not (len(local) >= 16 and given and hmac.compare_digest(given, local)):
            self.require(request)
            if request.method == "POST":
                self.check_csrf(request)
        if request.method == "POST":
            body = await self.body_json(request)
            try:
                self.updates.request(str(body.get("op") or ""), body.get("mode"))
            except ValueError as e:
                return json_error(400, str(e))
        return web.json_response(self.updates.state())

    # -- other machines (this one is their hub) -------------------------------

    PROXY_API = frozenset({"action", "preview", "projects", "upload", "update", "hooks"})

    def require_person(self, request: web.Request):
        """A passkey session: not a hub's link."""
        session = self.require(request)
        if getattr(session, "link", False):
            raise web.HTTPForbidden(text=json.dumps({"error": "not for links"}), content_type="application/json")
        return session

    async def api_machines_invite(self, request: web.Request) -> web.Response:
        """A one-time code for `lariat join` on a new machine (Add a machine)."""
        self.require_person(request)
        self.check_csrf(request)
        token = self.invites.create()
        pin = ""
        if self.cfg.cert_file and Path(self.cfg.cert_file).exists():
            try:  # our certificate is our own: the new machine checks it against this
                pin = pin_of_file(Path(self.cfg.cert_file))
            except (OSError, ValueError):
                pin = ""
        where = urlsplit(self.cfg.origin)
        host = (where.hostname or self.cfg.host).split(".")[0]  # `lariat join` finds the full name
        if where.port and where.port != 443:
            host += f":{where.port}"
        code = format_code(token + pin)
        return web.json_response({"code": code, "command": f"lariat join {host} {code}", "expires": INVITE_TTL})

    async def api_machines_join(self, request: web.Request) -> web.Response:
        """A machine adding itself (`lariat join HUB CODE`): the code is its only credential."""
        try:
            self._limit(request)
        except AuthError as e:
            return json_error(e.status, str(e))
        body = await self.body_json(request, 256 * 1024)
        token = normalize_code(str(body.get("code") or ""))[:TOKEN_LEN]
        if not self.invites.valid(token):
            self.auth.limiter.failure()
            log.warning("machine join refused from %s: wrong or expired code", remote_addr(request))
            return json_error(403, "that code is wrong or expired: make a new one on the phone")
        try:
            name = check_name(str(body.get("name") or ""))
            url = str(body.get("url") or "").rstrip("/")
            link_token = str(body.get("token") or "")
            ca = body.get("ca") or None
            if not (url.startswith("https://") or loopback(url)) or len(link_token) < 32 or not isinstance(ca, (str, type(None))):
                raise ValueError("incomplete join request")
        except ValueError as e:
            return json_error(400, str(e))
        same = [e for e in self.machines.registry.load() if e["name"] == name]
        if same and same[0]["url"] != url:
            return json_error(409, f"another machine is called {name} here: pass --name")
        try:
            await self.machines.check(url, link_token, ca)
        except ValueError as e:
            return json_error(502, str(e))
        if not self.invites.take(token):
            return json_error(403, "that code was just used")
        self.machines.registry.add(name, url, link_token, ca)
        self.machines.sync()
        log.info("machine %s joined (%s)", name, url)
        return web.json_response({"ok": True, "name": name, "hub": self.cfg.app_name})

    async def api_machines_remove(self, request: web.Request) -> web.Response:
        """Stop reaching a machine, and have it revoke our link."""
        self.require_person(request)
        self.check_csrf(request)
        name = str((await self.body_json(request)).get("name") or "")
        if not any(e["name"] == name for e in self.machines.registry.load()):
            return json_error(404, "no such machine")
        unlinked = await self.machines.unlink(name)
        self.machines.registry.remove(name)
        self.machines.sync()
        log.info("machine %s removed%s", name, "" if unlinked else " (it was not reachable to revoke the link)")
        return web.json_response({"ok": True, "unlinked": unlinked})

    async def api_hooks(self, request: web.Request) -> web.Response:
        """Agent hooks that report here (the alerts): seen, and installed, from the phone.
        POST {agent: claude|grok}. Anyone with a session has a shell here anyway."""
        from . import install

        self.require(request)
        if request.method == "POST":
            self.check_csrf(request)
            agent = (await self.body_json(request)).get("agent")
            try:
                if agent == "claude":
                    await asyncio.to_thread(install.install_claude_hooks, None, None, self.uploads.dir)
                elif agent == "grok":
                    await asyncio.to_thread(install.install_grok_hooks)
                else:
                    return json_error(400, "agent is claude or grok")
            except (OSError, ValueError) as e:
                return json_error(500, f"could not write the hooks: {e}")
            log.info("agent hooks installed: %s", agent)
        managed = install.MANAGED_HOOKS.exists()
        return web.json_response(
            {"claude": install.claude_hooks_installed(), "grok": install.grok_hooks_path().exists(), "managed": managed}
        )

    async def api_unlink(self, request: web.Request) -> web.Response:
        """A hub giving up its link here (the machine was removed on the phone)."""
        session = self.require(request)
        if not getattr(session, "link", False):
            return json_error(403, "only a linked hub can unlink itself")
        self.links.revoke(session.name)
        log.info("link %s removed by its hub", session.name)
        return web.json_response({"ok": True})

    async def proxy_api(self, request: web.Request) -> web.Response:
        """/m/<name>/api/<action|preview|projects|upload>, forwarded with that machine's link token."""
        self.require(request)
        if request.method == "POST":
            self.check_csrf(request)
        remote = self.machines.get(request.match_info["name"])
        tail = request.match_info["tail"]
        if remote is None or tail not in self.PROXY_API:
            return json_error(404, "no such machine")
        url = f"{remote.entry['url']}/api/{tail}" + (f"?{request.query_string}" if request.query_string else "")
        headers = dict(remote.headers)
        data = None
        if request.method == "POST":
            data = await request.read()
            headers["Content-Type"] = request.headers.get("Content-Type", "application/octet-stream")
        try:
            async with self.machines.http.request(
                request.method, url, data=data, headers=headers, ssl=remote.ssl, timeout=aiohttp.ClientTimeout(total=60)
            ) as r:
                body = await r.read()
                return web.Response(status=r.status, body=body, content_type=r.content_type or "application/json")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return json_error(502, f"{remote.name} is not reachable")

    async def proxy_term(self, request: web.Request) -> web.StreamResponse:
        """A terminal on another machine: this WebSocket piped to its /ws/term."""
        session = self.session(request)
        if session is None:
            return json_error(401, "login required")
        if request.headers.get("Origin") != self.cfg.origin and not request.get("lariat_link"):
            return json_error(403, "bad origin")
        remote = self.machines.get(request.match_info["name"])
        if remote is None:
            return json_error(404, "no such machine")
        if session.ptys >= MAX_PTYS_PER_SESSION:
            return json_error(429, "too many open terminals")
        base = remote.entry["url"]
        target = ("wss://" + base[8:] if base.startswith("https://") else "ws://" + base.split("://", 1)[-1]) + "/ws/term"
        if request.query_string:
            target += "?" + request.query_string
        ws = web.WebSocketResponse(max_msg_size=64 * 1024, heartbeat=30.0, compress=False)
        await ws.prepare(request)
        session.ptys += 1
        try:
            async with self.machines.http.ws_connect(
                target, headers=remote.headers, ssl=remote.ssl, heartbeat=30.0, max_msg_size=0, timeout=aiohttp.ClientWSTimeout(ws_close=5)
            ) as up:

                async def down() -> None:
                    async for msg in up:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            await ws.send_bytes(msg.data)
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            await ws.send_str(msg.data)
                        else:
                            return

                async def upward() -> None:
                    async for msg in ws:
                        self.sessions.touch(session)
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            await up.send_bytes(msg.data)
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            await up.send_str(msg.data)
                        else:
                            return

                async def watchdog() -> None:
                    while self.alive(session):
                        await asyncio.sleep(15)

                tasks = [asyncio.create_task(t()) for t in (down, upward, watchdog)]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if tasks[2] in done:
                    await ws.close(code=CLOSE_EXPIRED, message=b"session expired")
                elif tasks[0] in done and not ws.closed:
                    await ws.close(code=up.close_code or 1000)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            if not ws.closed:
                await ws.close(code=1011, message=f"{remote.name} unreachable".encode())
        finally:
            session.ptys -= 1
            if not ws.closed:
                await ws.close()
        return ws

    # -- static and health ----------------------------------------------

    async def healthz(self, request: web.Request) -> web.Response:
        peer = request.remote or ""
        forwarded = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
        try:
            # With our own TLS, tailscale serve forwards TCP and every client looks local.
            ok = ipaddress.ip_address(peer).is_loopback and not forwarded and not self.cfg.tls_cert
        except ValueError:
            ok = False
        if not ok:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True, "cert_days_left": cert_days_left(self.cfg.cert_file)})

    async def static_handler(self, request: web.Request) -> web.StreamResponse:
        path = request.match_info.get("path", "")
        if path in ("", "index.html"):
            return self.static.response(request, "index.html")
        if path == "enroll":
            # Served while enrollment is configured, even after the token is used,
            # so a scanned link says "already used" instead of a bare 404.
            if not self.cfg.enrollment:
                raise web.HTTPNotFound()
            return self.static.response(request, "index.html")
        return self.static.response(request, path)

    # -- lifecycle -------------------------------------------------------

    async def on_startup(self, app: web.Application) -> None:
        self._main = asyncio.current_task()  # run_app's: cancelling it shuts down cleanly
        for provider in (self.tmux, self.herdr, self.alerts):
            if provider is not None:
                try:
                    await provider.start()
                except Exception:
                    log.exception("provider %s failed to start", provider.name)
        self.uploads.start()
        await self.machines.start()
        await self.updates.start()
        if self.dictation is not None:
            # Load and warm the model now, not on the first word someone says.
            self._warming = asyncio.create_task(self.dictation.warm())

    async def on_cleanup(self, app: web.Application) -> None:
        self.uploads.stop()
        await self.machines.stop()
        await self.updates.stop()
        for provider in (self.tmux, self.herdr, self.alerts):
            if provider is not None:
                await provider.stop()
        if self.dictation is not None:
            await self.dictation.close()
        if self.push is not None:
            await self.push.close()

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self.headers_mw], client_max_size=UPLOAD_MAX + 1024)
        r = app.router
        r.add_get("/healthz", self.healthz)
        r.add_get("/auth/info", self.auth_info)
        r.add_post("/auth/login/options", self.login_options)
        r.add_post("/auth/login/verify", self.login_verify)
        r.add_post("/auth/logout", self.logout)
        r.add_post("/auth/register/options", self.register_options)
        r.add_post("/auth/register/verify", self.register_verify)
        r.add_get("/api/state", self.api_state)
        r.add_get("/api/events", self.api_events)
        r.add_get("/api/projects", self.api_projects)
        r.add_get("/api/preview", self.api_preview)
        r.add_post("/api/action", self.api_action)
        r.add_post("/api/dictate", self.api_dictate)
        r.add_post("/api/upload", self.api_upload)
        r.add_post("/api/push/subscribe", self.api_push_subscribe)
        r.add_post("/api/push/unsubscribe", self.api_push_unsubscribe)
        r.add_post("/api/push/test", self.api_push_test)
        r.add_post("/api/presence", self.api_presence)
        r.add_post("/api/notify", self.api_notify)
        r.add_post("/api/machines/invite", self.api_machines_invite)
        r.add_post("/api/machines/join", self.api_machines_join)
        r.add_post("/api/machines/remove", self.api_machines_remove)
        r.add_post("/api/unlink", self.api_unlink)
        r.add_get("/api/hooks", self.api_hooks)
        r.add_post("/api/hooks", self.api_hooks)
        r.add_get("/api/update", self.api_update)
        r.add_post("/api/update", self.api_update)
        r.add_get("/ws/term", self.ws_term)
        r.add_get("/ws/dictate", self.ws_dictate)
        r.add_get("/m/{name}/api/{tail}", self.proxy_api)
        r.add_post("/m/{name}/api/{tail}", self.proxy_api)
        r.add_get("/m/{name}/ws/term", self.proxy_term)
        r.add_get("/{path:.*}", self.static_handler)
        app.on_startup.append(self.on_startup)
        app.on_cleanup.append(self.on_cleanup)
        return app
