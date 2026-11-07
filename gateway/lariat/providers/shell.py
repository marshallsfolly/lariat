"""The plain shell provider: one "New shell" item, a fresh login shell per
terminal. Nothing persists after the terminal closes."""

from __future__ import annotations

import os

from ..config import Config


class ShellProvider:
    name = "shell"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def state(self) -> dict:
        return {"enabled": True, "command": os.path.basename(self.cfg.login_shell()[0])}

    def argv(self) -> list[str]:
        return self.cfg.login_shell()
