import contextlib
import io
import json
import os
import plistlib
import unittest
from pathlib import Path
from unittest import mock

from lariat import cli, doctor, install

from .helpers import TempDir

CMD = ["/opt/lariat/bin/lariat"]


class HooksTest(TempDir, unittest.TestCase):
    def test_merge_keeps_everything_else_and_is_idempotent(self):
        path = self.tmp / "settings.json"
        mine = {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}
        path.write_text(json.dumps(mine))
        self.assertTrue(install.install_claude_hooks(path, CMD))
        data = json.loads(path.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertEqual(len(data["hooks"]["Stop"]), 2)  # theirs, then ours
        self.assertIn("/opt/lariat/bin/lariat notify --hook claude stop", json.dumps(data))
        self.assertIn("notify --hook claude notification", json.dumps(data["hooks"]["Notification"]))
        self.assertEqual(json.loads((self.tmp / "settings.json.lariat-backup").read_text()), mine)
        self.assertFalse(install.install_claude_hooks(path, CMD))  # already there
        self.assertTrue(install.claude_hooks_installed(path))
        # A newer lariat elsewhere (a Nix update, a moved binary): ours are replaced, not added.
        self.assertTrue(install.install_claude_hooks(path, ["/new/bin/lariat"]))
        data = json.loads(path.read_text())
        self.assertEqual(len(data["hooks"]["Stop"]), 2)
        self.assertIn("/new/bin/lariat notify --hook claude stop", json.dumps(data))
        self.assertNotIn("/opt/lariat", json.dumps(data))
        self.assertIn("say done", json.dumps(data))

    def test_uploads_permission(self):
        path = self.tmp / "settings.json"
        uploads = Path.home() / ".local" / "state" / "lariat" / "uploads"
        install.install_claude_hooks(path, CMD, uploads=uploads)
        data = json.loads(path.read_text())
        if os.name == "posix":
            self.assertEqual(data["permissions"]["allow"], ["Read(~/.local/state/lariat/uploads/**)"])
            self.assertEqual(install.uploads_rule(Path("/srv/lariat/uploads")), "Read(//srv/lariat/uploads/**)")
        else:  # Claude Code's path rules differ on Windows; no rule there
            self.assertNotIn("permissions", data)
        self.assertFalse(install.install_claude_hooks(path, CMD, uploads=uploads))

    def test_fresh_settings_and_grok(self):
        path = self.tmp / "new" / "settings.json"
        self.assertTrue(install.install_claude_hooks(path, CMD))
        self.assertFalse((self.tmp / "new" / "settings.json.lariat-backup").exists())
        grok = install.install_grok_hooks(self.tmp, CMD)
        self.assertIn("notify --hook grok stop", grok.read_text())


class ServiceTest(TempDir, unittest.TestCase):
    def test_unit_files(self):
        argv = [*CMD, "serve", "--name", "box"]
        env = {"LARIAT_DICTATE_MODEL": "/m.bin", "PATH": "/usr/bin"}
        unit = install.systemd_unit(argv, env)
        self.assertIn("ExecStart=/opt/lariat/bin/lariat serve --name box", unit)
        self.assertIn('Environment="LARIAT_DICTATE_MODEL=/m.bin"', unit)
        plist = plistlib.loads(install.launchd_plist(argv, env, self.tmp / "p.log"))
        self.assertEqual(plist["ProgramArguments"], argv)
        self.assertEqual(plist["EnvironmentVariables"]["PATH"], "/usr/bin")
        cmd = install.windows_startup(argv, env)
        self.assertIn('set "LARIAT_DICTATE_MODEL=/m.bin"', cmd)
        self.assertIn('start "lariat" /min', cmd)
        lines = cmd.split("\r\n")  # started again after exiting for an update
        self.assertEqual(lines[lines.index(":loop") + 1 :][:2], ["/opt/lariat/bin/lariat serve --name box", "if %errorlevel%==75 goto loop"])

    def test_services_are_marked_supervised(self):
        with mock.patch.dict(os.environ, {"LARIAT_PORT": "9000"}, clear=False):
            env = install.service_env()
        self.assertEqual((env["LARIAT_SUPERVISED"], env["LARIAT_PORT"]), ("1", "9000"))

    def test_dry_run_writes_nothing(self):
        env = {"HOME": str(self.tmp), "USERPROFILE": str(self.tmp), "XDG_CONFIG_HOME": str(self.tmp / "cfg"), "APPDATA": str(self.tmp / "ad")}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            install.install_service(["--name", "box"], dry_run=True)
            self.assertFalse(install.service_installed())
        text = out.getvalue()
        for word in ("serve", "--name", "box"):  # one command line (systemd, Windows) or plist <string>s (macOS)
            self.assertIn(word, text)


class DoctorTest(TempDir, unittest.TestCase):
    def test_no_tailscale(self):
        with mock.patch.object(cli, "find_tailscale", return_value=None), \
             mock.patch.object(cli, "gateway_info", return_value=None), \
             mock.patch.object(doctor, "NIXOS_UNIT", Path(self.tmp / "none")), \
             mock.patch.object(install, "MANAGED_HOOKS", Path(self.tmp / "none")), \
             mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "claude")}):
            checks = doctor.run(self.tmp / "state", 8795)
        status = {c["id"]: c["status"] for c in checks}
        self.assertEqual(status["tailscale.installed"], "fail")
        self.assertEqual(status["https"], "warn")  # not fatal: lariat makes its own CA when it starts
        self.assertEqual(status["gateway"], "warn")
        self.assertTrue(all("fix" in c for c in checks if c["status"] in ("fail", "warn")))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(doctor.report(checks, as_json=True), 1)
        self.assertFalse(json.loads(out.getvalue())["ok"])


class ServiceArgsTest(unittest.TestCase):
    def test_dry_run_is_never_a_serve_option(self):
        with mock.patch.object(install, "install_service") as inst, mock.patch.object(install, "uninstall_service") as uninst:
            cli.main(["service", "install", "--dry-run", "--", "--name", "box"])
            inst.assert_called_once_with(["--name", "box"], dry_run=True)
            inst.reset_mock()
            cli.main(["service", "install", "--", "--name", "box"])
            inst.assert_called_once_with(["--name", "box"], dry_run=False)
            cli.main(["service", "uninstall", "--dry-run"])
            uninst.assert_called_once_with(dry_run=True)
