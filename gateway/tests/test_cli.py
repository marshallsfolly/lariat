import contextlib
import hashlib
import io
import os
import unittest
from unittest import mock

from lariat import cli
from lariat.config import load

from .helpers import TempDir


class CliTest(TempDir, unittest.TestCase):
    def test_token_is_kept_until_used_then_renewed(self):
        first = cli.fresh_token(self.tmp)
        self.assertGreaterEqual(len(first), 40)
        self.assertEqual(cli.fresh_token(self.tmp), first)
        if os.name == "posix":
            self.assertEqual(oct((self.tmp / "enroll-token").stat().st_mode & 0o777), "0o600")
        (self.tmp / "token-consumed").write_text(hashlib.sha256(first.encode()).hexdigest() + "\n")
        second = cli.fresh_token(self.tmp)
        self.assertNotEqual(second, first)
        self.assertNotEqual(cli.fresh_token(self.tmp, renew=True), second)

    def test_enroll_prints_the_link(self):
        out = io.StringIO()
        with mock.patch("shutil.which", return_value=None), contextlib.redirect_stdout(out):
            cli.main(["enroll", "--host", "box.example.ts.net", "--state", str(self.tmp)])
        token = (self.tmp / "enroll-token").read_text().strip()
        self.assertIn(f"https://box.example.ts.net/enroll#t={token}", out.getvalue())

    def test_bare_command_means_serve(self):
        with mock.patch.object(cli, "cmd_serve") as serve:
            cli.main(["--host", "h.example", "--no-tailscale-serve"])
        args = serve.call_args[0][0]
        self.assertEqual((args.cmd, args.host, args.no_tailscale_serve), ("serve", "h.example", True))

    def test_gateway_keeps_its_own_secrets(self):
        keep = {k: os.environ[k] for k in ("HOME", "USERPROFILE", "SYSTEMROOT") if k in os.environ}  # Path.home()
        env = {**keep, "LARIAT_HOST": "h.example", "LARIAT_STATE_DIR": str(self.tmp / "state")}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load()
            key = cfg.session_key_file.read_bytes()
            self.assertEqual(len(key), 48)
            self.assertEqual(load().session_key_file.read_bytes(), key)  # kept across restarts
            self.assertGreaterEqual(len(cfg.enroll_token_file.read_bytes()), 40)
            self.assertIsNone(cfg.credentials_file)


class SetupTest(TempDir, unittest.TestCase):
    """`lariat setup --yes`, with Tailscale, the service and the phone mocked."""

    def run_setup(self, *extra):
        from lariat import doctor, install

        calls = {"qr": [], "service": [], "join": []}
        node = {"name": "box.example.ts.net", "https": True, "tailscale": "tailscale", "ips": ["100.64.0.1"]}
        with mock.patch.object(cli, "tailnet", return_value=node), \
             mock.patch.object(cli, "operator_missing", return_value=False), \
             mock.patch.object(install, "install_service", side_effect=lambda a, **k: calls["service"].append((a, k))), \
             mock.patch.object(cli, "gateway_info", return_value={"enroll": True}), \
             mock.patch.object(cli, "show_qr", side_effect=lambda url, what: calls["qr"].append(url)), \
             mock.patch.object(cli, "cmd_join", side_effect=lambda a: calls["join"].append((a.hub, a.code))), \
             mock.patch.object(install, "claude_settings", return_value=self.tmp / "none" / "settings.json"), \
             mock.patch.object(cli.shutil, "which", return_value=None), \
             mock.patch.object(doctor, "run", return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["setup", "--yes", "--state", str(self.tmp / "state"), *extra])
        return calls, out.getvalue()

    def test_the_phones_machine(self):
        calls, out = self.run_setup()
        self.assertEqual(calls["service"], [([], {"quiet": True})])
        self.assertEqual(len(calls["qr"]), 1)
        self.assertIn("https://box.example.ts.net/enroll#t=", calls["qr"][0])
        self.assertIn("Push notifications", out)

    def test_another_machine_joins(self):
        calls, out = self.run_setup("--join", "desktop", "ABCD-EFGH-JKMN")
        self.assertEqual(calls["join"], [("desktop", "ABCD-EFGH-JKMN")])
        self.assertEqual(calls["qr"], [])  # no enrolling: the phone uses the hub
