import argparse
import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from lariat import cli
from lariat.app import Gateway

from .helpers import TempDir, make_config


def args(**kw):
    base = dict(kind="info", agent=None, project=None, title=None, message=[], hook=None)
    return argparse.Namespace(**{**base, **kw})


class NotifyTest(TempDir, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        static = self.tmp / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>t</title>")
        self.cfg = make_config(self.tmp, static_dir=static, push=False)
        self.state = self.tmp / "state"
        self.state.mkdir(exist_ok=True)
        (self.state / "notify-token").write_text("n" * 43)
        self.cfg.notify_token_file = self.state / "notify-token"
        self.gateway = Gateway(self.cfg)
        self.alerts = []
        self.gateway.hub.alert_listeners.append(self.alerts.append)
        self.server = TestServer(self.gateway.app())
        await self.server.start_server()
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_needs_the_token(self):
        r = await self.client.post("/api/notify", json={"message": "hi"})
        self.assertEqual(r.status, 403)
        r = await self.client.post("/api/notify", json={"message": "hi"}, headers={"X-Lariat-Notify": "wrong" * 9})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.alerts, [])

    async def test_lands_in_the_inbox_and_on_the_hub(self):
        r = await self.client.post(
            "/api/notify",
            json={"kind": "blocked", "agent": "claude", "project": "shop", "message": "Allow Bash?"},
            headers={"X-Lariat-Notify": "n" * 43},
        )
        self.assertEqual(r.status, 200)
        item = self.alerts[0]
        self.assertEqual((item["kind"], item["pushTitle"], item["pushBody"]), ("blocked", "claude needs you", "shop · Allow Bash?"))
        self.assertEqual(self.gateway.alerts.state()["items"][0]["message"], "Allow Bash?")
        self.assertFalse(self.gateway.alerts.state()["ntfy"])

    def test_hook_payloads(self):
        stop = cli.notify_body(args(hook=["claude", "stop"]), json.dumps({"cwd": "/home/a/code/shop", "last_assistant_message": "All\\n tests pass."}))
        self.assertEqual(stop, {"kind": "done", "agent": "claude", "project": "shop", "message": "All\\n tests pass."})
        note = cli.notify_body(args(hook=["claude", "notification"]), json.dumps({"cwd": "/x/web", "message": "Claude needs permission"}))
        self.assertEqual((note["kind"], note["project"], note["message"]), ("blocked", "web", "Claude needs permission"))
        self.assertEqual(cli.notify_body(args(hook=["grok", "stop"]), "not json")["message"], "finished")

    async def test_the_command_reaches_the_gateway(self):
        port = self.server.port
        body = cli.notify_body(args(kind="done", agent="claude", message=["built", "it"]))
        await asyncio.get_running_loop().run_in_executor(None, cli.post_notify, self.state, port, body)
        self.assertEqual(self.alerts[-1]["message"], "built it")
