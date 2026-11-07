import asyncio
import os
import time
import unittest
from pathlib import Path

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from lariat.app import Gateway
from lariat.uploads import MAX_BYTES, Uploads, image_type

from .helpers import HOST, ORIGIN, TempDir, make_config
from .softauthn import SoftAuthenticator
from .test_app import POST, TOKEN

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class UploadTest(TempDir, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        static = self.tmp / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>t</title>")
        self.cfg = make_config(self.tmp, static_dir=static, push=False)
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
        r = await self.client.post(
            "/auth/register/verify", headers={**POST, "X-Enroll-Token": TOKEN},
            json={"credential": key.create(await r.text()), "label": "test"},
        )
        self.assertEqual(r.status, 200)

    async def test_saves_an_image_and_returns_its_path(self):
        r = await self.client.post("/api/upload", data=JPEG, headers=POST)
        self.assertEqual(r.status, 401)
        await self.login()
        r = await self.client.post("/api/upload", data=JPEG, headers={"Origin": ORIGIN})
        self.assertEqual(r.status, 403)  # no CSRF header
        r = await self.client.post("/api/upload", data=JPEG, headers={**POST, "Content-Type": "image/jpeg"})
        self.assertEqual(r.status, 200)
        path = Path((await r.json())["path"])
        self.assertEqual(path.parent, self.cfg.state_dir / "uploads")
        self.assertEqual(path.suffix, ".jpg")
        self.assertEqual(path.read_bytes(), JPEG)
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_a_body_that_arrives_in_pieces_is_kept_whole(self):
        # Over a real network an upload arrives in chunks; all of them count.
        await self.login()
        image = JPEG + os.urandom(300 * 1024) + b"\xff\xd9"

        async def slowly():
            for i in range(0, len(image), 16 * 1024):
                yield image[i : i + 16 * 1024]
                await asyncio.sleep(0.002)

        r = await self.client.post("/api/upload", data=slowly(), headers={**POST, "Content-Type": "image/jpeg"})
        self.assertEqual(r.status, 200)
        self.assertEqual(Path((await r.json())["path"]).read_bytes(), image)

        async def too_much():
            yield PNG
            for _ in range(MAX_BYTES // (64 * 1024) + 2):  # no Content-Length to go by
                yield b"\x00" * 64 * 1024
                await asyncio.sleep(0)

        r = await self.client.post("/api/upload", data=too_much(), headers=POST)
        self.assertEqual(r.status, 413)

    async def test_refuses_what_is_not_an_image(self):
        await self.login()
        r = await self.client.post("/api/upload", data=b"#!/bin/sh\nrm -rf ~\n", headers=POST)
        self.assertEqual(r.status, 415)
        r = await self.client.post("/api/upload", data=PNG + b"\x00" * MAX_BYTES, headers=POST)
        self.assertEqual(r.status, 413)

    def test_types_and_sweep(self):
        self.assertEqual(image_type(PNG[:16]), "png")
        self.assertEqual(image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "webp")
        self.assertEqual(image_type(b"\x00\x00\x00\x18ftypheic"), "heic")
        self.assertIsNone(image_type(b"%PDF-1.7"))
        uploads = Uploads(self.tmp / "u", keep_days=1)
        old, new = uploads.save(JPEG), uploads.save(PNG)
        os.utime(old, (time.time() - 2 * 86400,) * 2)
        self.assertEqual(uploads.sweep(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
