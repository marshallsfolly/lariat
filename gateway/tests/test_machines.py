import asyncio
import json
import os
import sys
import unittest
from unittest import mock

import aiohttp
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from lariat import install, links, machines
from lariat.app import Gateway

from .helpers import HOST, ORIGIN, TempDir, make_config
from .softauthn import SoftAuthenticator
from .test_app import POST, TOKEN

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
ECHO = "import sys; print('hi', flush=True); x = sys.stdin.readline().strip(); print('got:' + x, flush=True)"


class LinkCodeTest(TempDir, unittest.TestCase):
    def test_tokens_and_codes(self):
        store = links.Links(self.tmp / "links.json")
        token = store.create("hub")
        self.assertNotIn(token, (self.tmp / "links.json").read_text())  # only the hash is kept
        self.assertEqual(store.session(token).cred_id, "link:hub")
        self.assertIsNone(store.session("x" * 43))
        code = links.encode_code("https://box.example.ts.net", token, "box", "-----BEGIN CERTIFICATE-----\n...")
        data = links.decode_code(code)
        self.assertEqual((data["url"], data["token"], data["name"]), ("https://box.example.ts.net", token, "box"))
        self.assertTrue(data["ca"].startswith("-----BEGIN"))
        with self.assertRaises(ValueError):
            links.decode_code("lariat-link:" + "garbage")
        self.assertTrue(store.revoke("hub"))
        self.assertIsNone(store.session(token))
        # Another process (the gateway) sees a revoke at once, even within one clock tick.
        other = links.Links(self.tmp / "links.json")
        token = store.create("hub")
        self.assertIsNotNone(other.session(token))
        stamp = (self.tmp / "links.json").stat().st_mtime_ns
        store.revoke("hub")
        os.utime(self.tmp / "links.json", ns=(stamp, stamp))  # same time as before the revoke
        self.assertIsNone(other.session(token))

    def test_states_do_not_nest(self):
        self.assertEqual(machines.own_state({"herdr": 1, "machines": [{"name": "hub", "state": {}}]}), {"herdr": 1})
        self.assertIsNone(machines.own_state(None))

    def test_forwarded_alert(self):
        item = machines.forwarded_alert("box", {"id": "7", "kind": "blocked", "pushTitle": "api needs you", "url": "/#go=agent:default:w1:p1"})
        self.assertEqual((item["machine"], item["pushTitle"], item["url"]), ("box", "box: api needs you", "/#go=m:box:agent:default:w1:p1"))


class HubTest(TempDir, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        static = self.tmp / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>t</title>")
        # The other machine ("box").
        (self.tmp / "node").mkdir()
        (self.tmp / "node" / "notify-token").write_text("n" * 43)
        self.node_cfg = make_config(
            self.tmp / "node", static_dir=static, push=False, shell_command=[sys.executable, "-c", ECHO], notify_token_file=self.tmp / "node" / "notify-token"
        )
        self.node = Gateway(self.node_cfg)
        self.node_server = TestServer(self.node.app(), shutdown_timeout=0.5)  # its event stream to the hub never ends by itself
        await self.node_server.start_server()
        self.token = self.node.links.create("hub")
        # The hub: the machine the phone uses.
        (self.tmp / "hub").mkdir()
        self.hub_cfg = make_config(self.tmp / "hub", static_dir=static, push=False)
        machines.Registry(self.hub_cfg.state_dir / "machines.json").add(
            "box", str(self.node_server.make_url("")).rstrip("/"), self.token, None
        )
        self.hub = Gateway(self.hub_cfg)
        hub_server = TestServer(self.hub.app(), shutdown_timeout=0.5)
        await hub_server.start_server()
        jar = CookieJar(unsafe=True, treat_as_secure_origin=[str(hub_server.make_url("/")).rstrip("/")])
        self.client = TestClient(hub_server, cookie_jar=jar)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.node_server.close()

    async def login(self):
        key = SoftAuthenticator(HOST, ORIGIN)
        r = await self.client.post("/auth/register/options", headers={**POST, "X-Enroll-Token": TOKEN})
        r = await self.client.post(
            "/auth/register/verify", headers={**POST, "X-Enroll-Token": TOKEN},
            json={"credential": key.create(await r.text()), "label": "test"},
        )
        self.assertEqual(r.status, 200)

    async def connected(self):
        async with asyncio.timeout(10):
            while True:
                box = next((m for m in self.hub.machines.state() if m["name"] == "box"), None)
                if box and box["connected"] and box["state"] is not None:
                    return box
                await asyncio.sleep(0.05)

    async def test_the_node_takes_the_link_token(self):
        bearer = {"Authorization": f"Bearer {self.token}"}
        async with aiohttp.ClientSession(str(self.node_server.make_url(""))) as c:  # not TestClient: closing it stops the server
            r = await c.get("/api/state", headers=bearer)
            self.assertEqual(r.status, 200)
            r = await c.post("/api/upload", data=JPEG, headers=bearer)  # no CSRF header needed with a bearer token
            self.assertEqual(r.status, 200)
            r = await c.get("/api/state", headers={"Authorization": "Bearer " + "x" * 43})
            self.assertEqual(r.status, 401)
            self.node.links.revoke("hub")
            r = await c.get("/api/state", headers=bearer)
            self.assertEqual(r.status, 401)

    async def test_hub_follows_state_and_forwards_alerts(self):
        box = await self.connected()
        self.assertEqual(box["config"]["appName"], "lariat")
        self.node.alerts.add({"id": "a1", "time": 9999999999, "kind": "done", "title": "api - shop", "pushTitle": "api is done", "message": "ok"})
        async with asyncio.timeout(10):
            while not self.hub.alerts.items:
                await asyncio.sleep(0.05)
        item = self.hub.alerts.items[-1]
        self.assertEqual((item["machine"], item["pushTitle"]), ("box", "box: api is done"))

    async def test_proxy_api_and_upload(self):
        await self.connected()
        r = await self.client.get("/m/box/api/projects")
        self.assertEqual(r.status, 401)
        await self.login()
        r = await self.client.get("/m/box/api/projects")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertIn("roots", await r.json())
        r = await self.client.post("/m/box/api/upload", data=JPEG, headers=POST)
        self.assertEqual(r.status, 200)
        path = (await r.json())["path"]
        self.assertTrue(path.startswith(str(self.node_cfg.state_dir / "uploads")))  # it lands on that machine
        r = await self.client.get("/m/box/api/state")
        self.assertEqual(r.status, 404)  # only the proxied endpoints
        r = await self.client.get("/m/nope/api/projects")
        self.assertEqual(r.status, 404)

    async def test_proxy_terminal(self):
        await self.connected()
        await self.login()
        ws = await self.client.ws_connect("/m/box/ws/term?k=shell&c=80&r=24", headers={"Origin": ORIGIN})
        hello = json.loads(await ws.receive_str(timeout=10))
        self.assertEqual(hello["t"], "hello")
        out = b""
        while b"hi" not in out:
            out += await ws.receive_bytes(timeout=10)
        await ws.send_bytes(b"abc\r")
        while b"got:abc" not in out:
            msg = await ws.receive(timeout=10)
            if isinstance(msg.data, bytes):
                out += msg.data
        await ws.close()

    async def test_unreachable_machine(self):
        await self.connected()
        await self.login()
        machines.Registry(self.hub_cfg.state_dir / "machines.json").add("gone", "http://127.0.0.1:9", "t" * 43, None)
        async with asyncio.timeout(10):
            while "gone" not in self.hub.machines.remotes:
                await asyncio.sleep(0.05)
        r = await self.client.get("/m/gone/api/projects")
        self.assertEqual(r.status, 502)
        r = await self.client.get("/api/state")
        gone = next(m for m in (await r.json())["state"]["machines"] if m["name"] == "gone")
        self.assertFalse(gone["connected"])

    async def test_updates_from_the_phone_and_the_cli(self):
        await self.connected()
        r = await self.client.get("/api/update")
        self.assertEqual(r.status, 401)
        await self.login()
        r = await self.client.get("/api/update")
        self.assertIn(r.status, (200,))
        self.assertIn((await r.json())["kind"], ("pip", "nix"))
        nix = self.node.updates.how == "nix"  # the tests also run inside the Nix build
        r = await self.client.post("/m/box/api/update", json={"op": "mode", "mode": "notify"}, headers=POST)  # the phone, through the hub
        self.assertEqual(r.status, 400 if nix else 200)
        self.assertEqual(self.node.updates.state()["mode"], "off" if nix else "notify")
        r = await self.client.post("/api/update", json={"op": "later"}, headers=POST)
        self.assertEqual(r.status, 400)
        token = self.node_cfg.notify_token_file.read_text().strip()  # `lariat update` on that machine
        async with aiohttp.ClientSession(str(self.node_server.make_url(""))) as c:  # not TestClient: closing it stops the server
            r = await c.get("/api/update", headers={"X-Lariat-Notify": token})
            self.assertEqual(r.status, 200)
            r = await c.get("/api/update", headers={"X-Lariat-Notify": "x" * 43})
            self.assertEqual(r.status, 401)
        async with asyncio.timeout(10):  # the hub sees the machine's update state
            while (await self.connected())["state"].get("update", {}).get("mode") != self.node.updates.state()["mode"]:
                await asyncio.sleep(0.05)

    async def test_add_a_machine_with_a_code(self):
        await self.connected()
        r = await self.client.post("/api/machines/invite", json={}, headers=POST)
        self.assertEqual(r.status, 401)
        await self.login()
        r = await self.client.post("/api/machines/invite", json={}, headers=POST)
        invite = await r.json()
        self.assertTrue(invite["command"].startswith("lariat join ") and invite["command"].endswith(invite["code"]))
        url = str(self.node_server.make_url("")).rstrip("/")
        token = self.node.links.create("hub2")
        join = {"code": invite["code"], "name": "box2", "url": url, "token": token}
        r = await self.client.post("/api/machines/join", json={**join, "code": "AAAA-AAAA-AAAA"})
        self.assertEqual(r.status, 403)
        r = await self.client.post("/api/machines/join", json={**join, "name": "box", "url": "http://127.0.0.1:9"})
        self.assertEqual(r.status, 409)  # another machine has that name
        r = await self.client.post("/api/machines/join", json={**join, "url": "https://example.com"})
        self.assertEqual(r.status, 502)  # the hub checks the machine answers first
        r = await self.client.post("/api/machines/join", json=join)
        self.assertEqual(r.status, 200)
        r = await self.client.post("/api/machines/join", json=join)
        self.assertEqual(r.status, 403)  # once only
        async with asyncio.timeout(10):
            while not any(m["name"] == "box2" and m["connected"] for m in self.hub.machines.state()):
                await asyncio.sleep(0.05)
        r = await self.client.post("/api/machines/remove", json={"name": "box2"}, headers=POST)
        self.assertEqual((r.status, (await r.json())["unlinked"]), (200, True))
        self.assertFalse(self.node.links.active("hub2"))  # the machine revoked the hub's link
        self.assertNotIn("box2", [e["name"] for e in self.hub.machines.registry.load()])

    async def test_agent_hooks_from_the_phone(self):
        await self.connected()
        await self.login()
        home = self.tmp / "home"
        env = {"HOME": str(home), "USERPROFILE": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude")}  # never the real settings
        with mock.patch.dict(os.environ, env), mock.patch.object(install, "MANAGED_HOOKS", self.tmp / "none.json"):
            r = await self.client.get("/m/box/api/hooks")
            self.assertEqual((await r.json())["claude"], False)
            r = await self.client.post("/m/box/api/hooks", json={"agent": "claude"}, headers=POST)  # on that machine, through the hub
            self.assertEqual((await r.json())["claude"], True)
            r = await self.client.post("/m/box/api/hooks", json={"agent": "codex"}, headers=POST)
            self.assertEqual(r.status, 400)
