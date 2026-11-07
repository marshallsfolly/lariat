"""Input validation shared by the providers, the API and the PTY bridge.

Everything that reaches argv or a herdr request passes through here first.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

TMUX_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
AGENT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
HERDR_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
HERDR_SESSION = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
AGENT_KIND = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

# Logical key names herdr's agent.send_keys accepts that the UI may send.
QUICK_KEYS = frozenset({"esc", "enter", "ctrl+c", "tab", "up", "down", "left", "right", "y", "n", "1", "2", "3"})


class Invalid(ValueError):
    """Bad client input. The message is safe to return as a 400."""


def tmux_name(value) -> str:
    if not isinstance(value, str) or not TMUX_NAME.match(value):
        raise Invalid("session names are 1-32 letters, digits, '-' or '_'")
    return value


def agent_name(value) -> str:
    if not isinstance(value, str) or not AGENT_NAME.match(value):
        raise Invalid("agent names start with a lowercase letter: a-z, 0-9, '-' or '_', up to 32")
    return value


def herdr_id(value) -> str:
    if not isinstance(value, str) or not HERDR_ID.match(value):
        raise Invalid("bad id")
    return value


def herdr_session(value) -> str:
    if value is None or value == "":
        return "default"
    if not isinstance(value, str) or not HERDR_SESSION.match(value):
        raise Invalid("bad session name")
    return value


DEVICE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def device(value) -> str:
    """A device id the app makes up and keeps in localStorage."""
    if not isinstance(value, str) or not DEVICE.match(value):
        raise Invalid("bad device id")
    return value


def label(value, limit: int = 64) -> str:
    if not isinstance(value, str):
        raise Invalid("name must be a string")
    value = value.strip()
    if not value or len(value) > limit or not value.isprintable():
        raise Invalid(f"names are 1-{limit} printable characters")
    return value


def text(value, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise Invalid(f"text must be 1-{limit} characters")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
        raise Invalid("text may not contain control characters")
    return value


def keys(value) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise Invalid("keys must be a list of 1-16 keys")
    for key in value:
        if key not in QUICK_KEYS:
            raise Invalid(f"key not allowed: {key!r}")
    return list(value)


def cwd_under(value, roots: list[Path]) -> Path:
    """Resolve a directory and require it to be one of the roots or inside one."""
    if not isinstance(value, str) or not value or "\0" in value:
        raise Invalid("pick a directory")
    path = Path(os.path.expanduser(value))
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise Invalid("that directory does not exist") from e
    if not real.is_dir():
        raise Invalid("that is not a directory")
    for root in roots:
        try:
            root_real = root.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if real == root_real or root_real in real.parents:
            return real
    raise Invalid("directory is outside the project roots")


def prefix_bytes(spec: str) -> str:
    """'ctrl+b' / 'C-b' / 'ctrl+space' -> the control character a terminal sends."""
    s = spec.strip().lower()
    for lead in ("ctrl+", "ctrl-", "c-"):
        if s.startswith(lead):
            key = s[len(lead):]
            break
    else:
        return spec if len(spec) == 1 else "\x02"
    if key in ("space", "@", "2"):
        return "\x00"
    if len(key) == 1 and "a" <= key <= "z":
        return chr(ord(key) - ord("a") + 1)
    table = {"[": "\x1b", "\\": "\x1c", "]": "\x1d", "^": "\x1e", "_": "\x1f", "/": "\x1f"}
    return table.get(key, "\x02")
