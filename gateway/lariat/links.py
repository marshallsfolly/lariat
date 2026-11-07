"""Links between machines: one lariat (the hub, the one the phone uses) reaches
the others, so the phone needs one app, one passkey and one notification
subscription for all of them.

On a machine, `lariat link` makes a link code: the machine's URL, a random
token and, when it uses its own CA, that CA's certificate (so the hub can pin
it). The machine keeps only the token's hash (state/links.json). The hub sends
the token as a bearer token; the machine then treats the request as a logged-in
session for the API and terminals, never for login or enrollment. Revoking the
link ends it at once, open terminals included.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

PREFIX = "lariat-link:"


@dataclass
class LinkSession:
    """A hub, authenticated by its link token. Shaped like sessions.Session."""

    name: str
    sid: str = "link"
    created: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    ptys: int = 0
    meta: dict = field(default_factory=dict)
    link: bool = True

    @property
    def cred_id(self) -> str:
        return f"link:{self.name}"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def encode_code(url: str, token: str, name: str, ca_pem: str | None) -> str:
    data = {"v": 1, "url": url, "token": token, "name": name}
    if ca_pem:
        data["ca"] = ca_pem
    raw = json.dumps(data, separators=(",", ":")).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_code(code: str) -> dict:
    code = code.strip()
    if not code.startswith(PREFIX):
        raise ValueError("not a lariat link code (it starts with lariat-link:)")
    body = code[len(PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError as e:
        raise ValueError("this link code is damaged; copy it again") from e
    if not isinstance(data, dict) or not str(data.get("url", "")).startswith("https://") or len(data.get("token", "")) < 32:
        raise ValueError("this link code is incomplete")
    return data


class Links:
    def __init__(self, path: Path):
        self.path = path
        self._cache: tuple[tuple, list[dict]] = ((), [])
        self._sessions: dict[str, LinkSession] = {}

    def load(self) -> list[dict]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return []
        # Every save replaces the file (a new inode / file ID): the time alone can
        # repeat within one clock tick (Windows), and a revoke must never be missed.
        key = (st.st_ino, st.st_size, st.st_mtime_ns)
        if key != self._cache[0]:
            try:
                data = json.loads(self.path.read_text())
                self._cache = (key, [d for d in data if isinstance(d, dict) and "hash" in d and "name" in d])
            except ValueError:
                self._cache = (key, [])
        return self._cache[1]

    def _save(self, links: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(links, f, indent=1)
        os.replace(tmp, self.path)
        self._cache = ((), [])

    def create(self, name: str) -> str:
        """A new token for `name` (replacing an old one of that name)."""
        token = secrets.token_urlsafe(32)
        links = [d for d in self.load() if d["name"] != name]
        links.append({"name": name, "hash": _hash(token), "created": int(time.time())})
        self._save(links)
        return token

    def revoke(self, name: str) -> bool:
        links = self.load()
        kept = [d for d in links if d["name"] != name]
        if len(kept) == len(links):
            return False
        self._save(kept)
        return True

    def active(self, name: str) -> bool:
        return any(d["name"] == name for d in self.load())

    def sessions(self) -> list[LinkSession]:
        return list(self._sessions.values())

    def session(self, token: str) -> LinkSession | None:
        digest = _hash(token)
        for d in self.load():
            if hmac.compare_digest(digest, d["hash"]):
                return self._sessions.setdefault(d["name"], LinkSession(d["name"]))
        return None
