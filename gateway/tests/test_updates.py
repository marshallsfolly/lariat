import asyncio
import hashlib
import os
import sys
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestServer

from lariat import updates

from .helpers import TempDir

POSIX = os.name == "posix"
NEW = b"#!/bin/sh\necho lariat 9.9.9\n" if POSIX else b"MZ fake"


class FakeHub:
    def __init__(self):
        self.changes = 0

    def changed(self):
        self.changes += 1


class Versions(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(updates.version_tuple("v0.10.2"), (0, 10, 2))
        self.assertEqual(updates.version_tuple("nonsense"), ())
        self.assertTrue(updates.is_newer("0.10.0", "0.9.9"))
        self.assertFalse(updates.is_newer("0.4.0", "0.4.0"))
        self.assertFalse(updates.is_newer(None, "0.4.0"))

    def test_asset_names(self):
        self.assertEqual(updates.asset_name("linux", "aarch64"), "lariat-linux-arm64")
        self.assertEqual(updates.asset_name("darwin", "arm64"), "lariat-macos-arm64")
        self.assertEqual(updates.asset_name("win32", "AMD64"), "lariat-windows-x86_64.exe")
        self.assertIsNone(updates.asset_name("linux", "riscv64"))

    def test_default_modes(self):
        with mock.patch.dict(os.environ, {"LARIAT_UPDATES": ""}):
            self.assertEqual(updates.default_mode("binary"), "auto")
            self.assertEqual(updates.default_mode("pip"), "notify")
            self.assertEqual(updates.default_mode("nix"), "off")  # never checks: the flake updates it


class Release(TempDir, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sums = hashlib.sha256(NEW).hexdigest()
        app = web.Application()
        app.router.add_get("/releases/latest", lambda r: web.HTTPFound("/releases/tag/v9.9.9"))
        app.router.add_get("/releases/tag/v9.9.9", lambda r: web.Response(text="release page"))
        app.router.add_get("/releases/download/v9.9.9/SHA256SUMS", lambda r: web.Response(text=f"{self.sums}  {updates.asset_name()}\n"))
        app.router.add_get(f"/releases/download/v9.9.9/{updates.asset_name()}", lambda r: web.Response(body=NEW))
        self.server = TestServer(app)
        await self.server.start_server()
        self.base = str(self.server.make_url("")).rstrip("/")
        self.target = self.tmp / ("lariat.exe" if sys.platform == "win32" else "lariat")
        self.target.write_bytes(b"old")

    async def asyncTearDown(self):
        await self.server.close()

    async def test_latest_and_verified_download(self):
        self.assertEqual(await asyncio.to_thread(updates.latest_version, self.base), "9.9.9")
        new = await asyncio.to_thread(updates.download, "9.9.9", self.tmp, self.base, POSIX)  # POSIX: runs its --version
        self.assertEqual(new.read_bytes(), NEW)
        self.sums = "0" * 64
        with self.assertRaises(updates.UpdateError):
            await asyncio.to_thread(updates.download, "9.9.9", self.tmp, self.base, False)
        self.assertEqual(sorted(p.name for p in self.tmp.glob(".lariat-update-*")), [new.name])  # the bad one is gone

    async def test_waits_for_quiet_then_restarts(self):
        busy = [True]
        restarted = []
        up = updates.Updater(
            self.tmp / "state", FakeHub(), busy=lambda: busy[0], restart=lambda: restarted.append(1),
            how="binary", target=self.target, base=self.base, current="0.4.0", is_supervised=lambda: True, run_check=POSIX,
        )
        with mock.patch.object(updates, "IDLE_POLL", 0.05):
            job = asyncio.create_task(up.apply(wait_idle=True))
            async with asyncio.timeout(10):
                while up.status != "waiting":
                    await asyncio.sleep(0.02)
            self.assertEqual(self.target.read_bytes(), b"old")  # a terminal is open: not yet
            busy[0] = False
            await asyncio.wait_for(job, 10)
        self.assertEqual(self.target.read_bytes(), NEW)
        self.assertEqual((up.status, up.installed, restarted), ("restarting", "9.9.9", [1]))
        self.assertFalse(up.state()["available"])

    async def test_unsupervised_only_installs(self):
        up = updates.Updater(
            self.tmp / "state", FakeHub(), busy=lambda: False, restart=lambda: self.fail("no service manager to start it again"),
            how="binary", target=self.target, base=self.base, current="0.4.0", is_supervised=lambda: False, run_check=POSIX,
        )
        up.request("apply")
        await asyncio.wait_for(up._job, 10)
        self.assertEqual((up.status, self.target.read_bytes()), ("restart", NEW))

    async def test_up_to_date_and_nix(self):
        up = updates.Updater(self.tmp / "s", FakeHub(), busy=lambda: False, restart=lambda: None, how="binary", target=self.target, base=self.base, current="9.9.9")
        await up.apply(wait_idle=False)
        self.assertEqual((up.status, self.target.read_bytes()), ("idle", b"old"))
        nix = updates.Updater(self.tmp / "n", FakeHub(), busy=lambda: False, restart=lambda: None, how="nix", base=self.base, current="0.4.0")
        await nix.check()
        self.assertIsNone(nix.latest)  # never asked
        self.assertEqual((nix.state()["mode"], nix.state()["canApply"]), ("off", False))

    async def test_switching_to_auto_updates_once_quiet(self):
        busy = [True]
        up = updates.Updater(
            self.tmp / "state", FakeHub(), busy=lambda: busy[0], restart=lambda: None,
            how="binary", target=self.target, base=self.base, current="0.4.0", is_supervised=lambda: True, run_check=POSIX,
        )
        with mock.patch.object(updates, "IDLE_POLL", 0.05):
            up.request("mode", "auto")
            async with asyncio.timeout(10):
                while up.status != "waiting":
                    await asyncio.sleep(0.02)
            up.request("apply")  # "Update now": no more waiting
            await asyncio.wait_for(up._job, 10)
        self.assertEqual((up.status, self.target.read_bytes()), ("restarting", NEW))

    def test_modes_persist(self):
        up = updates.Updater(self.tmp / "s", FakeHub(), busy=lambda: False, restart=lambda: None, how="pip")
        self.assertEqual(up.state()["mode"], "off")  # LARIAT_UPDATES in the tests
        up.request("mode", "notify")
        self.assertEqual(updates.Updater(self.tmp / "s", FakeHub(), busy=lambda: False, restart=lambda: None, how="pip").state()["mode"], "notify")
        with self.assertRaises(ValueError):
            up.request("mode", "sometimes")


class Install(TempDir, unittest.TestCase):
    def test_windows_renames_the_running_exe_aside(self):
        target, new = self.tmp / "lariat.exe", self.tmp / ".lariat-update-x.exe"
        target.write_bytes(b"old")
        new.write_bytes(b"new")
        updates.install(new, target, windows=True)
        self.assertEqual((target.read_bytes(), updates.aside_path(target).read_bytes()), (b"new", b"old"))
        updates.clean_up(target)
        self.assertFalse(updates.aside_path(target).exists())
