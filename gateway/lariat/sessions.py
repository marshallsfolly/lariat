"""Login sessions: a random ID in a signed cookie, the state in memory.

The table lives only in this process, so a gateway restart logs everyone
out (re-login is one Face ID prompt). The HMAC key comes from the
session-key secret, so a forged or truncated cookie is rejected before any
table lookup.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class Session:
    sid: str
    cred_id: str
    created: float
    last_seen: float
    ptys: int = 0
    meta: dict = field(default_factory=dict)


class SessionStore:
    def __init__(self, key: bytes, idle_seconds: int, absolute_seconds: int, clock=time.monotonic):
        if len(key) < 16:
            raise ValueError("session key must be at least 16 bytes")
        self._key = key
        self.idle = idle_seconds
        self.absolute = absolute_seconds
        self._clock = clock
        self._table: dict[str, Session] = {}

    def _sign(self, sid: str) -> str:
        return b64u(hmac.new(self._key, sid.encode(), hashlib.sha256).digest())

    def create(self, cred_id: str) -> tuple[Session, str]:
        sid = b64u(secrets.token_bytes(32))
        now = self._clock()
        session = Session(sid=sid, cred_id=cred_id, created=now, last_seen=now)
        self._table[sid] = session
        self._prune()
        return session, f"{sid}.{self._sign(sid)}"

    def get(self, cookie: str | None, touch: bool = True) -> Session | None:
        if not cookie or cookie.count(".") != 1:
            return None
        sid, sig = cookie.split(".")
        if not hmac.compare_digest(sig, self._sign(sid)):
            return None
        session = self._table.get(sid)
        if session is None:
            return None
        now = self._clock()
        if now - session.created > self.absolute or now - session.last_seen > self.idle:
            self._table.pop(sid, None)
            return None
        if touch:
            session.last_seen = now
        return session

    def alive(self, session: Session) -> bool:
        """True while the session is still in the table and not expired (no touch)."""
        current = self._table.get(session.sid)
        if current is not session:
            return False
        now = self._clock()
        if now - session.created > self.absolute or now - session.last_seen > self.idle:
            self._table.pop(session.sid, None)
            return False
        return True

    def touch(self, session: Session) -> None:
        if self._table.get(session.sid) is session:
            session.last_seen = self._clock()

    def revoke(self, session: Session) -> None:
        self._table.pop(session.sid, None)

    def _prune(self) -> None:
        now = self._clock()
        for sid, s in list(self._table.items()):
            if now - s.created > self.absolute or now - s.last_seen > self.idle:
                del self._table[sid]

    def all(self) -> list[Session]:
        return list(self._table.values())

    def __len__(self) -> int:
        return len(self._table)
