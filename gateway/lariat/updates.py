"""Updates from lariat's releases on GitHub.

A release binary can replace itself: it finds the latest release through
github.com's /releases/latest redirect, downloads this platform's binary and
the release's SHA256SUMS, checks the hash, runs the new binary's --version,
then swaps it in (atomically; on Windows the running .exe is renamed aside
first). The gateway then exits with RESTART_CODE and its service manager starts
the new binary. Installs from Nix or pip update the way they were installed;
for pip this only reports a newer release, and Nix installs never check.

Modes (state/update.json, set from the phone or `lariat update --mode`):
"auto" checks every few hours and installs when no terminal is open,
"notify" only checks, "off" never contacts GitHub.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Callable

from . import __version__, tls

log = logging.getLogger("lariat.updates")

REPO = os.environ.get("LARIAT_UPDATE_REPO", "marshallsfolly/lariat")
BASE = f"https://github.com/{REPO}"
RESTART_CODE = 75  # EX_TEMPFAIL: "start me again" to systemd, launchd and the Windows loop
CHECK_EVERY = 6 * 3600.0
FIRST_CHECK = 60.0
IDLE_POLL = 30.0
MAX_SIZE = 300 * 1024 * 1024
MODES = ("auto", "notify", "off")


class UpdateError(Exception):
    pass


def kind() -> str:
    """How this lariat was installed: "binary" (a release), "nix" or "pip"."""
    if getattr(sys, "frozen", False):
        return "binary"
    if str(Path(__file__).resolve()).startswith("/nix/store/"):
        return "nix"
    return "pip"


def asset_name(system: str = sys.platform, machine: str | None = None) -> str | None:
    """This platform's binary in a release."""
    machine = (machine or platform.machine()).lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    osname = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(system)
    if not arch or not osname:
        return None
    return f"lariat-{osname}-{arch}" + (".exe" if osname == "windows" else "")


def version_tuple(v: str | None) -> tuple[int, ...]:
    m = re.match(r"v?(\d+(?:\.\d+)*)$", (v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def is_newer(latest: str | None, current: str) -> bool:
    new, cur = version_tuple(latest), version_tuple(current)
    return bool(new) and bool(cur) and new > cur


def self_binary() -> Path:
    return Path(sys.executable)  # a release binary: the file itself


def supervised() -> bool:
    """Does a service manager start lariat again when it exits with RESTART_CODE?"""
    from .install import LABEL, service_installed

    if os.environ.get("LARIAT_SUPERVISED") == "1":  # set by `lariat service install`
        return True
    if sys.platform == "darwin":
        return os.environ.get("XPC_SERVICE_NAME") == LABEL  # launchd, KeepAlive
    if sys.platform != "win32":
        return "INVOCATION_ID" in os.environ and service_installed()  # systemd, Restart=on-failure
    return False


def _open(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": f"lariat/{__version__}"})
    return urllib.request.urlopen(req, timeout=timeout, context=tls.public_context())


def latest_version(base: str = BASE) -> str:
    """The latest release's version, from where /releases/latest redirects."""
    with _open(base + "/releases/latest", 20) as r:
        final = r.geturl()
    m = re.search(r"/releases/tag/v?(\d+(?:\.\d+)*)/?$", final)
    if not m:
        raise UpdateError("no release found")
    return m.group(1)


def download(version: str, dest_dir: Path, base: str = BASE, run_check: bool = True) -> Path:
    """Fetch and verify this platform's binary of `version`, next to the current one."""
    name = asset_name()
    if not name:
        raise UpdateError(f"no release binary for {sys.platform} {platform.machine()}")
    url = f"{base}/releases/download/v{version}/"
    with _open(url + "SHA256SUMS", 30) as r:
        sums = r.read(1 << 20).decode(errors="replace")
    want = None
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            want = parts[0].lower()
    if not want:
        raise UpdateError(f"release {version} has no {name}")
    fd, tmp_name = tempfile.mkstemp(prefix=".lariat-update-", suffix=".exe" if name.endswith(".exe") else "", dir=dest_dir)
    tmp = Path(tmp_name)
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as f, _open(url + name, 300) as r:
            while chunk := r.read(1 << 16):
                size += len(chunk)
                if size > MAX_SIZE:
                    raise UpdateError("the download is too big")
                digest.update(chunk)
                f.write(chunk)
        if digest.hexdigest() != want:
            raise UpdateError("the download does not match the release's SHA256SUMS")
        os.chmod(tmp, 0o755)
        if run_check:
            try:
                out = subprocess.run([str(tmp), "--version"], capture_output=True, text=True, timeout=120).stdout
            except (OSError, subprocess.SubprocessError) as e:
                raise UpdateError(f"the new binary does not run: {e}") from e
            if f"lariat {version}" not in out:
                raise UpdateError(f"the new binary reports {out.strip()[:60]!r}, not lariat {version}")
        return tmp
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


def aside_path(target: Path) -> Path:
    return target.with_name(f"{target.stem}.old{target.suffix}")


def install(new: Path, target: Path, windows: bool = sys.platform == "win32") -> None:
    """Put `new` where `target` is. Windows cannot overwrite a running .exe, but
    it can rename it: the old one moves aside and is deleted on the next start."""
    if windows:
        aside = aside_path(target)
        with suppress(FileNotFoundError):
            aside.unlink()
        os.replace(target, aside)
        try:
            os.replace(new, target)
        except OSError:
            os.replace(aside, target)
            raise
    else:
        os.replace(new, target)


def clean_up(target: Path) -> None:
    """Leftovers of earlier updates: the renamed .exe, unfinished downloads."""
    with suppress(OSError):
        aside_path(target).unlink()
    with suppress(OSError):
        for p in target.parent.glob(".lariat-update-*"):
            if time.time() - p.stat().st_mtime > 3600:
                p.unlink()


class Settings:
    def __init__(self, path: Path, default: str):
        self.path = path
        self.default = default

    def mode(self) -> str:
        try:
            mode = json.loads(self.path.read_text()).get("mode")
        except (OSError, ValueError, AttributeError):
            mode = None
        return mode if mode in MODES else self.default

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode is one of {', '.join(MODES)}")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.write_text(json.dumps({"mode": mode}) + "\n")


def default_mode(how: str) -> str:
    env = os.environ.get("LARIAT_UPDATES")
    if how == "nix":
        return "off"  # updated with the flake input, never checks
    if env in MODES:
        return env
    return "auto" if how == "binary" else "notify"


class Updater:
    """The gateway's side: checks, downloads, waits for quiet, installs, restarts."""

    name = "update"

    def __init__(
        self,
        state_dir: Path,
        hub,
        busy: Callable[[], bool],
        restart: Callable[[], None],
        *,
        how: str | None = None,
        target: Path | None = None,
        base: str | None = None,
        current: str = __version__,
        is_supervised: Callable[[], bool] = supervised,
        run_check: bool = True,
    ):
        self.how = how or kind()
        self.settings = Settings(state_dir / "update.json", default_mode(self.how))
        self.hub = hub
        self.busy = busy
        self.restart = restart
        self.target = target or (self_binary() if self.how == "binary" else None)
        self.base = base or BASE
        self.current = current
        self.is_supervised = is_supervised
        self.run_check = run_check
        self.latest: str | None = None
        self.installed: str | None = None  # waiting for a restart
        self.status = "idle"  # checking, downloading, waiting, installing, restarting, restart, error
        self.error: str | None = None
        self.checked: float | None = None
        self._now = False
        self._task: asyncio.Task | None = None
        self._job: asyncio.Task | None = None

    def can_apply(self) -> bool:
        return self.how == "binary" and self.target is not None and asset_name() is not None

    def state(self) -> dict:
        return {
            "kind": self.how,
            "current": self.current,
            "latest": self.latest,
            "available": is_newer(self.latest, self.current) and self.installed != self.latest,
            "mode": "off" if self.how == "nix" else self.settings.mode(),
            "canApply": self.can_apply(),
            "status": self.status,
            "error": self.error,
            "installed": self.installed,
            "checked": self.checked,
        }

    def _set(self, status: str, error: str | None = None) -> None:
        self.status, self.error = status, error
        self.hub.changed()

    async def check(self) -> None:
        if self.how == "nix":
            return
        self._set("checking")
        try:
            self.latest = await asyncio.to_thread(latest_version, self.base)
            self.checked = time.time()
            self._set("restart" if self.installed else "idle")
        except (UpdateError, OSError, ValueError) as e:
            self._set("error", f"could not check for updates: {e}")

    async def apply(self, wait_idle: bool) -> None:
        """Download, (wait until no terminal is open,) install and restart."""
        if not self.can_apply():
            self._set("error", "this install updates the way it was installed (Nix or pip)")
            return
        try:
            if not wait_idle or not is_newer(self.latest, self.current):
                await self.check()
            version = self.latest
            if not is_newer(version, self.current) or self.installed == version:
                return
            self._set("downloading")
            new = await asyncio.to_thread(download, version, self.target.parent, self.base, self.run_check)
            if wait_idle and self.busy() and not self._now:
                self._set("waiting")  # for the open terminals to close: a restart ends them
                while self.busy() and not self._now:
                    await asyncio.sleep(IDLE_POLL)
            self._set("installing")
            await asyncio.to_thread(install, new, self.target)
            self.installed = version
            log.info("installed lariat %s (was %s)", version, self.current)
            if self.is_supervised():
                self._set("restarting")
                await asyncio.sleep(1.5)  # let the phone see it
                self.restart()
            else:
                self._set("restart")  # the new binary starts with the next `lariat serve`
        except (UpdateError, OSError, ValueError) as e:
            log.warning("update failed: %s", e)
            self._set("error", str(e))

    async def cycle(self) -> None:
        """A check, then (in auto mode) the update, once no terminal is open."""
        await self.check()
        if self.settings.mode() == "auto" and self.can_apply() and self.state()["available"]:
            await self.apply(wait_idle=True)

    def start_job(self, make) -> bool:
        """Run make() unless a job is running (it is only created when started)."""
        if self._job and not self._job.done():
            return False
        self._job = asyncio.create_task(make())
        return True

    def request(self, op: str, mode: str | None = None) -> None:
        """From the phone or `lariat update`: check, apply (now) or set the mode."""
        if self.how == "nix":
            raise ValueError("this lariat comes from Nix: it updates with your flake input")
        if op == "mode":
            self.settings.set_mode(mode or "")
            self.hub.changed()
            if mode == "auto":
                self._now = False
                self.start_job(self.cycle)
        elif op == "check":
            self.start_job(self.cycle)
        elif op == "apply":
            self._now = True  # also cuts short an auto update waiting for quiet
            self.start_job(lambda: self.apply(wait_idle=False))
        else:
            raise ValueError("op is check, apply or mode")

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_CHECK)
        while True:
            if self.settings.mode() != "off":
                self._now = False
                self.start_job(self.cycle)
            await asyncio.sleep(CHECK_EVERY)

    async def start(self) -> None:
        if self.target is not None:
            await asyncio.to_thread(clean_up, self.target)
        if self.how != "nix":
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for task in (self._task, self._job):
            if task:
                task.cancel()
