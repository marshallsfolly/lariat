"""Images from the phone, for agents to read.

The app shrinks a picked or pasted image (which also drops its metadata,
GPS included) and posts it; it is saved under <state>/uploads with a name
lariat makes up, and the app types its path into the session. Only images
are accepted (checked by their first bytes), and old uploads are deleted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from pathlib import Path

log = logging.getLogger("lariat.uploads")

MAX_BYTES = 20 * 1024 * 1024
SWEEP_EVERY = 6 * 3600


def image_type(head: bytes) -> str | None:
    """The file extension for these first bytes, if they start an image we take."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1"):
        return "heic"
    return None


class Uploads:
    def __init__(self, directory: Path, keep_days: float):
        self.dir = directory
        self.keep = keep_days * 86400
        self._task: asyncio.Task | None = None

    def save(self, data: bytes) -> Path:
        ext = image_type(data[:16])
        if ext is None:
            raise ValueError("not an image")
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.{ext}"
        path = self.dir / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path

    def sweep(self) -> int:
        """Delete uploads older than keep_days. Returns how many."""
        if self.keep <= 0 or not self.dir.is_dir():
            return 0
        cutoff = time.time() - self.keep
        gone = 0
        for path in self.dir.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    gone += 1
            except OSError:
                pass
        return gone

    def start(self) -> None:
        async def loop() -> None:
            while True:
                gone = self.sweep()
                if gone:
                    log.info("uploads: deleted %d older than %g days", gone, self.keep / 86400)
                await asyncio.sleep(SWEEP_EVERY)

        self._task = asyncio.create_task(loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
