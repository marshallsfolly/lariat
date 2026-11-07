import asyncio
import json
import sys
import unittest

from lariat.hub import Hub
from lariat.providers import ActionError
from lariat.providers.herdr import HerdrProvider, HerdrSession, normalize
from lariat.validate import Invalid

from .helpers import TempDir, make_config

SNAPSHOT = {
    "focused_pane_id": "w1:p2",
    "workspaces": [
        {"workspace_id": "w2", "number": 2, "label": "other", "focused": False, "agent_status": "idle"},
        {"workspace_id": "w1", "number": 1, "label": "proj", "focused": True, "agent_status": "blocked"},
    ],
    "tabs": [
        {"tab_id": "w1:t1", "workspace_id": "w1", "number": 1, "label": "1", "focused": True},
        {"tab_id": "w2:t1", "workspace_id": "w2", "number": 1, "label": "1", "focused": False},
    ],
    "panes": [
        {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1", "agent": None, "cwd": "/home/a/dev/proj"},
        {
            "pane_id": "w1:p2",
            "tab_id": "w1:t1",
            "workspace_id": "w1",
            "agent": "claude",
            "agent_status": "blocked",
            "cwd": "/home/a/dev/proj/",
            "focused": True,
        },
        {"pane_id": "w2:p1", "tab_id": "w2:t1", "workspace_id": "w2", "agent": "grok", "agent_status": "done"},
    ],
    "agents": [
        {
            "pane_id": "w1:p2",
            "agent": "claude",
            "agent_status": "blocked",
            "name": "rev",
            "terminal_title_stripped": "Reviewing",
            "completion_seq": 2,
            "state_change_seq": 5,
        },
        {"pane_id": "w2:p1", "agent": "grok", "agent_status": "done"},
    ],
}


class NormalizeTest(unittest.TestCase):
    def test_shape(self):
        state = normalize(SNAPSHOT)
        self.assertEqual([w["id"] for w in state["workspaces"]], ["w1", "w2"])
        panes = state["workspaces"][0]["tabs"][0]["panes"]
        self.assertEqual(panes[0]["agent"], None)
        self.assertEqual(panes[0]["status"], None)
        self.assertEqual(panes[1]["name"], "rev")
        self.assertEqual(panes[1]["status"], "blocked")
        self.assertEqual(panes[1]["cwd"], "proj")
        self.assertEqual(panes[1]["title"], "Reviewing")
        self.assertEqual(state["focused_pane"], "w1:p2")


class FakeHerdr:
    """Answers herdr JSON requests on a unix socket and records them."""

    def __init__(self, path):
        self.path = path
        self.calls = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_unix_server(self.handle, path=str(self.path))

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        line = await reader.readline()
        req = json.loads(line)
        self.calls.append((req["method"], req["params"]))
        method = req["method"]
        if method == "session.snapshot":
            result = {"type": "session_snapshot", "snapshot": SNAPSHOT}
        elif method == "events.subscribe":
            writer.write(json.dumps({"id": req["id"], "result": {"type": "subscription_started"}}).encode() + b"\n")
            await writer.drain()
            await asyncio.sleep(3600)
            return
        elif method == "workspace.create":
            result = {"type": "workspace_created", "root_pane": {"pane_id": "w3:p1"}}
        elif method == "pane.read":
            result = {"type": "pane_read", "read": {"text": "line1\nline2\n"}}
        else:
            result = {"type": "ok"}
        writer.write(json.dumps({"id": req["id"], "result": result}).encode() + b"\n")
        await writer.drain()
        writer.close()


@unittest.skipIf(sys.platform == "win32", "uses unix sockets")
class ProviderTest(TempDir, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (self.tmp / "dev" / "proj").mkdir(parents=True)
        self.cfg = make_config(
            self.tmp, herdr="/fake/herdr", agent_kinds=["claude"], project_roots=[self.tmp / "dev"]
        )
        self.fake = FakeHerdr(self.tmp / "h.sock")
        await self.fake.start()
        self.provider = HerdrProvider(self.cfg, Hub())
        session = HerdrSession(self.provider, "default", str(self.tmp / "h.sock"))
        self.provider.sessions["default"] = session
        await session.refresh()

    async def asyncTearDown(self):
        await self.fake.stop()

    def methods(self):
        return [m for m, _ in self.fake.calls if m != "session.snapshot"]

    async def test_argv(self):
        self.assertEqual(self.provider.argv_agent("default", "w1:p2"), ["/fake/herdr", "agent", "attach", "w1:p2"])
        self.assertEqual(self.provider.argv_tui(None), ["/fake/herdr"])
        with self.assertRaises(ActionError):
            self.provider.argv_agent("default", "w1:p1")  # no agent in that pane
        with self.assertRaises(Invalid):
            self.provider.argv_agent("default", "w1:p2; rm -rf /")
        with self.assertRaises(ActionError):
            self.provider.argv_agent("nosuch", "w1:p2")

    async def test_keys_and_focus(self):
        await self.provider.action("keys", {"target": "w1:p2", "keys": ["esc"]})
        await self.provider.action("focus", {"target": "w1:p2"})
        self.assertEqual(self.methods(), ["agent.send_keys", "agent.focus"])
        self.assertEqual(self.fake.calls[1][1], {"target": "w1:p2", "keys": ["esc"]})

    async def test_rejects_unknown_ids_and_ops(self):
        with self.assertRaises(ActionError):
            await self.provider.action("close_pane", {"target": "w9:p9"})
        with self.assertRaises(ActionError):
            await self.provider.action("server.stop", {})
        with self.assertRaises(Invalid):
            await self.provider.action("keys", {"target": "w1:p2", "keys": ["ctrl+z"]})
        self.assertEqual(self.methods(), [])

    async def test_close_workspace_never_groups(self):
        await self.provider.action("close_workspace", {"id": "w2"})
        self.assertEqual(self.fake.calls[-2], ("workspace.close", {"workspace_id": "w2"}))

    async def test_new_agent(self):
        result = await self.provider.action(
            "new_agent", {"kind": "claude", "cwd": str(self.tmp / "dev" / "proj"), "name": "proj-claude"}
        )
        self.assertEqual(result["pane"], "w3:p1")
        self.assertEqual(self.methods(), ["workspace.create", "agent.start"])
        with self.assertRaises(ActionError):
            await self.provider.action("new_agent", {"kind": "codex", "cwd": str(self.tmp / "dev"), "name": "x"})
        with self.assertRaises(Invalid):
            await self.provider.action("new_agent", {"kind": "claude", "cwd": "/etc", "name": "x"})

    async def test_preview(self):
        self.assertEqual(await self.provider.preview("default", "w1:p1"), "line1\nline2")

    async def test_state(self):
        state = self.provider.state()
        self.assertEqual(state["sessions"][0]["counts"]["blocked"], 1)
        self.assertEqual(state["sessions"][0]["counts"]["done"], 1)


if __name__ == "__main__":
    unittest.main()
