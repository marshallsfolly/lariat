import os
import unittest

from lariat import validate

from .helpers import TempDir


class ValidateTest(TempDir, unittest.TestCase):
    def test_tmux_names(self):
        self.assertEqual(validate.tmux_name("main_1-a"), "main_1-a")
        for bad in ["", "a" * 33, "a b", "a;b", "a.b", "a:b", None, 3, "$(x)"]:
            with self.assertRaises(validate.Invalid, msg=repr(bad)):
                validate.tmux_name(bad)

    def test_agent_names(self):
        validate.agent_name("claude-2")
        for bad in ["Claude", "2x", "", "a" * 33, "a b"]:
            with self.assertRaises(validate.Invalid):
                validate.agent_name(bad)

    def test_herdr_ids(self):
        validate.herdr_id("w3:p1")
        for bad in ["", "w3 p1", "w3;p1", "x" * 65, None]:
            with self.assertRaises(validate.Invalid):
                validate.herdr_id(bad)

    def test_keys(self):
        self.assertEqual(validate.keys(["esc", "ctrl+c"]), ["esc", "ctrl+c"])
        for bad in [[], ["ctrl+z"], ["esc"] * 17, "esc", [1]]:
            with self.assertRaises(validate.Invalid):
                validate.keys(bad)

    def test_text(self):
        validate.text("hello\nworld\t!")
        for bad in ["", "x" * 4001, "a\x1bb", None]:
            with self.assertRaises(validate.Invalid):
                validate.text(bad)

    def test_cwd_under(self):
        root = self.tmp / "dev"
        (root / "proj" / "sub").mkdir(parents=True)
        (self.tmp / "outside").mkdir()
        os.symlink(self.tmp / "outside", root / "escape")
        self.assertEqual(validate.cwd_under(str(root / "proj"), [root]), (root / "proj").resolve())
        self.assertEqual(validate.cwd_under(str(root), [root]), root.resolve())
        for bad in [str(self.tmp / "outside"), str(root / "escape"), str(root / "proj" / ".." / ".."), "/nonexistent", ""]:
            with self.assertRaises(validate.Invalid, msg=bad):
                validate.cwd_under(bad, [root])

    def test_prefix_bytes(self):
        self.assertEqual(validate.prefix_bytes("ctrl+b"), "\x02")
        self.assertEqual(validate.prefix_bytes("C-a"), "\x01")
        self.assertEqual(validate.prefix_bytes("ctrl+space"), "\x00")
        self.assertEqual(validate.prefix_bytes("ctrl+]"), "\x1d")


if __name__ == "__main__":
    unittest.main()
