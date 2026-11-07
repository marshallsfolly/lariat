import asyncio
import json
import math
import sys
import unittest
from array import array
from pathlib import Path

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from lariat.app import Gateway

from .helpers import HOST, ORIGIN, TempDir, make_config
from .softauthn import SoftAuthenticator
from .test_app import POST, TOKEN

FAKE = Path(__file__).with_name("fake_whisper.py")


def tone(seconds: float) -> bytes:
    n = int(16000 * seconds)
    return array("h", (int(8000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(n))).tobytes()


@unittest.skipIf(sys.platform == "win32", "uses a /bin/sh wrapper")
class DictationTest(TempDir, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        static = self.tmp / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>t</title>")
        server_bin = self.tmp / "whisper-server"
        server_bin.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        server_bin.chmod(0o755)
        self.log = self.tmp / "requests.jsonl"
        self.cfg = make_config(
            self.tmp,
            static_dir=static,
            dictate=True,
            whisper=str(server_bin),
            whisper_model="model.bin",
            dictate_args=["--fake-log", str(self.log)],
        )
        self.gateway = Gateway(self.cfg)
        server = TestServer(self.gateway.app())
        await server.start_server()
        jar = CookieJar(unsafe=True, treat_as_secure_origin=[str(server.make_url("/")).rstrip("/")])
        self.client = TestClient(server, cookie_jar=jar)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def login(self):
        key = SoftAuthenticator(HOST, ORIGIN)
        r = await self.client.post("/auth/register/options", headers={**POST, "X-Enroll-Token": TOKEN})
        options = await r.text()
        r = await self.client.post(
            "/auth/register/verify",
            headers={**POST, "X-Enroll-Token": TOKEN},
            json={"credential": key.create(options), "label": "test"},
        )
        self.assertEqual(r.status, 200, await r.text())

    def requests(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    async def until(self, ws, kind: str, timeout: float = 10.0) -> dict:
        async with asyncio.timeout(timeout):
            while True:
                msg = await ws.receive_json()
                if msg["t"] == kind:
                    return msg
                self.assertNotEqual(msg["t"], "error", msg)

    async def test_requires_login_and_origin(self):
        r = await self.client.get("/ws/dictate", headers={"Origin": ORIGIN})
        self.assertEqual(r.status, 401)
        await self.login()
        r = await self.client.get("/ws/dictate", headers={"Origin": "https://evil.test"})
        self.assertEqual(r.status, 403)

    async def test_stream_partials_then_final(self):
        await self.login()
        ws = await self.client.ws_connect("/ws/dictate", headers={"Origin": ORIGIN})
        await self.until(ws, "ready")
        audio = tone(1.2)
        step = 3200  # 100 ms
        for i in range(0, len(audio), step):
            await ws.send_bytes(audio[i : i + step])
            await asyncio.sleep(0.1)
        partial = await self.until(ws, "partial")
        self.assertRegex(partial["text"], r"^heard \d\.\ds$")
        await ws.send_str(json.dumps({"t": "stop"}))
        final = await self.until(ws, "final")
        self.assertEqual(final["text"], "heard 1.2s")
        self.assertFalse(final["limit"])
        self.assertEqual((await ws.receive()).type.name, "CLOSE")

        reqs = self.requests()
        self.assertEqual(reqs[0]["seconds"], 0.5)  # warm-up
        self.assertEqual(reqs[-1]["seconds"], 1.2)
        self.assertEqual(reqs[-1]["temperature_inc"], "0.2")  # the final may fall back
        self.assertTrue(all(r["temperature_inc"] == "0" for r in reqs[:-1]))  # partials do not
        self.assertTrue(all(r["prompt"] == "" for r in reqs))

    async def test_silence_never_reaches_the_model(self):
        await self.login()
        ws = await self.client.ws_connect("/ws/dictate", headers={"Origin": ORIGIN})
        await self.until(ws, "ready")
        await ws.send_bytes(bytes(32000))
        await asyncio.sleep(0.7)
        await ws.send_str(json.dumps({"t": "stop"}))
        final = await self.until(ws, "final")
        self.assertEqual(final["text"], "")
        self.assertEqual(len(self.requests()), 1)  # only the warm-up

    async def test_cap_finalizes(self):
        self.cfg.dictate_max_seconds = 1
        await self.login()
        ws = await self.client.ws_connect("/ws/dictate", headers={"Origin": ORIGIN})
        await ws.send_bytes(tone(1.5))
        final = await self.until(ws, "final")
        self.assertEqual(final["text"], "heard 1.0s")
        self.assertTrue(final["limit"])

    async def test_cancel_and_idle_stop(self):
        self.cfg.dictate_idle_minutes = 0.01  # 0.6 s
        await self.login()
        ws = await self.client.ws_connect("/ws/dictate", headers={"Origin": ORIGIN})
        await self.until(ws, "ready")
        self.assertTrue(self.gateway.dictation.ready)
        await ws.send_bytes(tone(0.3))
        await ws.send_str(json.dumps({"t": "cancel"}))
        self.assertEqual((await ws.receive()).type.name, "CLOSE")
        self.assertEqual(len(self.requests()), 1)
        await asyncio.sleep(1.5)
        self.assertFalse(self.gateway.dictation.ready)
        self.assertIsNone(self.gateway.dictation.proc)

    async def test_warm_at_start_and_revived_after_a_crash(self):
        from lariat import dictate

        self.addCleanup(setattr, dictate, "REVIVE_AFTER", dictate.REVIVE_AFTER)
        dictate.REVIVE_AFTER = 0.2
        d = self.gateway.dictation
        async with asyncio.timeout(10):
            while not d.ready:  # warmed at startup, before anyone records
                await asyncio.sleep(0.05)
        self.assertEqual(len(self.requests()), 1)
        d.proc.kill()
        await asyncio.sleep(0.3)
        async with asyncio.timeout(10):
            while not d.ready:
                await asyncio.sleep(0.05)
        self.assertEqual(len(self.requests()), 2)  # warmed up again
