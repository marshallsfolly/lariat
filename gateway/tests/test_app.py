import json
import sys
import unittest

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from lariat.app import Gateway

from .helpers import HOST, ORIGIN, TempDir, make_config
from .softauthn import SoftAuthenticator

TOKEN = "tok-" + "x" * 28
POST = {"Origin": ORIGIN, "X-Lariat": "1"}


class AppTest(TempDir, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        static = self.tmp / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>t</title>")
        (static / "app.css").write_text("body{}")
        self.cfg = make_config(self.tmp, static_dir=static)
        self.gateway = Gateway(self.cfg)
        server = TestServer(self.gateway.app())
        await server.start_server()
        # The cookies are Secure/__Host- (the real origin is https); let the
        # jar send them to the plain-http test server.
        jar = CookieJar(unsafe=True, treat_as_secure_origin=[str(server.make_url("/")).rstrip("/")])
        self.client = TestClient(server, cookie_jar=jar)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def login(self):
        key = SoftAuthenticator(HOST, ORIGIN)
        r = await self.client.post("/auth/register/options", headers={**POST, "X-Enroll-Token": TOKEN})
        self.assertEqual(r.status, 200)
        options = await r.text()
        r = await self.client.post(
            "/auth/register/verify",
            headers={**POST, "X-Enroll-Token": TOKEN},
            json={"credential": key.create(options), "label": "test"},
        )
        self.assertEqual(r.status, 200, await r.text())
        return key

    async def test_static_and_headers(self):
        r = await self.client.get("/")
        self.assertEqual(r.status, 200)
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        etag = r.headers["ETag"]
        r = await self.client.get("/", headers={"If-None-Match": etag})
        self.assertEqual(r.status, 304)
        r = await self.client.get("/nope.js")
        self.assertEqual(r.status, 404)
        r = await self.client.get("/app.css")
        self.assertTrue(r.headers["Content-Type"].startswith("text/css"))

    async def test_api_requires_login(self):
        for path in ("/api/state", "/api/events", "/api/projects"):
            r = await self.client.get(path)
            self.assertEqual(r.status, 401, path)
            self.assertEqual(r.headers["Cache-Control"], "no-store")
            self.assertIn("error", await r.json())
        r = await self.client.post("/api/action", headers=POST, json={"op": "x"})
        self.assertEqual(r.status, 401)

    async def test_ws_requires_login_and_origin(self):
        r = await self.client.get("/ws/term?k=shell", headers={"Origin": ORIGIN})
        self.assertEqual(r.status, 401)
        await self.login()
        r = await self.client.get("/ws/term?k=shell", headers={"Origin": "https://evil.test"})
        self.assertEqual(r.status, 403)
        r = await self.client.get("/ws/term?k=nope", headers={"Origin": ORIGIN})
        self.assertEqual(r.status, 404)

    async def test_csrf(self):
        r = await self.client.post("/auth/login/options")
        self.assertEqual(r.status, 403)
        r = await self.client.post("/auth/login/options", headers={"Origin": ORIGIN})
        self.assertEqual(r.status, 403)
        r = await self.client.post("/auth/login/options", headers={"Origin": "https://evil.test", "X-Lariat": "1"})
        self.assertEqual(r.status, 403)
        r = await self.client.post("/auth/login/options", headers=POST)
        self.assertEqual(r.status, 200)

    async def test_enroll_login_logout(self):
        key = await self.login()  # enrollment logs in
        r = await self.client.get("/api/state")
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertTrue(data["config"]["providers"]["shell"])
        self.assertFalse(data["config"]["providers"]["herdr"])

        r = await self.client.post("/auth/logout", headers=POST)
        self.assertEqual(r.status, 200)
        r = await self.client.get("/api/state")
        self.assertEqual(r.status, 401)

        r = await self.client.post("/auth/login/options", headers=POST)
        options = await r.text()
        r = await self.client.post("/auth/login/verify", headers=POST, json={"credential": key.get(options)})
        self.assertEqual(r.status, 200, await r.text())
        r = await self.client.get("/api/state")
        self.assertEqual(r.status, 200)

        # Once the token is used the page stays (it explains), enrollment does not.
        r = await self.client.get("/enroll")
        self.assertEqual(r.status, 200)
        r = await self.client.get("/auth/info")
        self.assertFalse((await r.json())["enroll"])
        r = await self.client.post("/auth/register/options", headers={**POST, "X-Enroll-Token": TOKEN})
        self.assertEqual(r.status, 403)

    async def test_enroll_page_only_while_enabled(self):
        r = await self.client.get("/enroll")
        self.assertEqual(r.status, 200)
        self.cfg.enrollment = False
        r = await self.client.get("/enroll")
        self.assertEqual(r.status, 404)

    async def test_healthz_loopback_only(self):
        r = await self.client.get("/healthz")
        self.assertEqual(r.status, 200)
        r = await self.client.get("/healthz", headers={"X-Forwarded-For": "100.64.0.9"})
        self.assertEqual(r.status, 404)

    async def test_shell_terminal(self):
        # Python as the "shell", so the same test drives a POSIX PTY and Windows ConPTY.
        script = "import sys; print('hi', flush=True); x = sys.stdin.readline().strip(); print('got:' + x, flush=True)"
        self.cfg.shell_command = [sys.executable, "-c", script]
        await self.login()
        ws = await self.client.ws_connect("/ws/term?k=shell&c=80&r=24", headers={"Origin": ORIGIN})
        hello = json.loads((await ws.receive_str()))
        self.assertEqual(hello["t"], "hello")
        out = b""
        while b"hi" not in out:
            out += await ws.receive_bytes(timeout=5)
        await ws.send_bytes(b"abc\r")
        while b"got:abc" not in out:
            msg = await ws.receive(timeout=5)
            if isinstance(msg.data, bytes):
                out += msg.data
        exited = None
        while exited is None:
            msg = await ws.receive(timeout=5)
            if isinstance(msg.data, str):
                exited = json.loads(msg.data)
        self.assertEqual(exited, {"t": "exit", "code": 0})
        await ws.close()

    async def test_pty_cap(self):
        self.cfg.shell_command = [sys.executable, "-c", "import time; time.sleep(30)"]
        await self.login()
        sockets = []
        for _ in range(4):
            ws = await self.client.ws_connect("/ws/term?k=shell", headers={"Origin": ORIGIN})
            await ws.receive_str()
            sockets.append(ws)
        r = await self.client.get("/ws/term?k=shell", headers={"Origin": ORIGIN})
        self.assertEqual(r.status, 429)
        for ws in sockets:
            await ws.close()


if __name__ == "__main__":
    unittest.main()
