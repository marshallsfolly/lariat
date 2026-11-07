from __future__ import annotations

import os
import tempfile
from pathlib import Path

from lariat.config import Config

os.environ.setdefault("LARIAT_UPDATES", "off")  # test gateways never ask GitHub

HOST = "lariat.test"
ORIGIN = "https://lariat.test"


def make_config(tmp: Path, **overrides) -> Config:
    (tmp / "session-key").write_bytes(os.urandom(48))
    (tmp / "enroll-token").write_text("tok-" + "x" * 28 + "\n")
    (tmp / "credentials").write_text("[]")
    cfg = Config(
        host=HOST,
        origin=ORIGIN,
        state_dir=tmp / "state",
        runtime_dir=tmp / "run",
        session_key_file=tmp / "session-key",
        enroll_token_file=tmp / "enroll-token",
        credentials_file=tmp / "credentials",
        enrollment=True,
        tmux=None,
        herdr=None,
        project_roots=[tmp],
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class TempDir:
    def setUp(self):  # noqa: N802 (unittest naming)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        super().setUp()

    def tearDown(self):  # noqa: N802
        super().tearDown()
        self._tmp.cleanup()
