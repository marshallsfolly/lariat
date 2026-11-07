"""Web Push to the installed app, so alerts arrive while it is closed.

A push goes from this host straight to the push service of the device's
browser (Apple's for an iPhone, Google's or Mozilla's elsewhere): there is
no other way to wake a closed app on iOS. The payload is encrypted end to
end (RFC 8291, aes128gcm) with keys only the subscribed device holds, so the
push service sees size and timing, not content. Requests are signed with a
VAPID key (RFC 8292) generated on first use and kept in the state dir.

Devices report when the app is in front (/api/presence); those get the
in-app alert instead of a push. The same alert from two sources (a herdr
status change and an ntfy hook) is pushed once.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import tls
from .config import Config

log = logging.getLogger("lariat.push")

# Push services we send to; anything else is refused (no requests to arbitrary URLs).
PUSH_HOSTS = ("push.apple.com", "fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com")
RECORD_SIZE = 4096
TTL = 6 * 3600  # an alert older than this is not worth showing
PRESENT_FOR = 15.0  # seconds a "visible" report counts (the app repeats it every 10 s)
DEDUPE_FOR = 10.0  # the same event reported twice (herdr and a hook), not two turns
STALE_AFTER = 120  # seconds: older alerts are history, not news
MAX_SUBSCRIPTIONS = 20
KINDS = ("blocked", "done", "info")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    text = "".join(text.split())
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def raw_public(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(
    plaintext: bytes,
    ua_public: bytes,
    auth_secret: bytes,
    *,
    salt: bytes | None = None,
    as_private: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
    """RFC 8291 message encryption: one aes128gcm record with its header."""
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = raw_public(as_private.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = as_private.exchange(ec.ECDH(), ua_key)
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = HKDF(hashes.SHA256(), 32, salt=auth_secret, info=key_info).derive(ecdh_secret)
    salt = salt or os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    if len(plaintext) + 1 + 16 > RECORD_SIZE:
        raise ValueError("push payload too large")
    record = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)  # 0x02: last record, no padding
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + record


class Vapid:
    """The application server key (P-256), created on first use."""

    def __init__(self, path: Path, subject: str):
        self.subject = subject
        if path.exists():
            self.key = serialization.load_pem_private_key(path.read_bytes(), None)
        else:
            self.key = ec.generate_private_key(ec.SECP256R1())
            pem = self.key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
        assert isinstance(self.key, ec.EllipticCurvePrivateKey)
        self.public = b64u(raw_public(self.key.public_key()))
        self._cache: dict[str, tuple[float, str]] = {}

    def authorization(self, endpoint: str) -> str:
        parts = urlsplit(endpoint)
        audience = f"{parts.scheme}://{parts.netloc}"
        now = time.time()
        cached = self._cache.get(audience)
        if cached is None or cached[0] - now < 3600:
            exp = int(now) + 12 * 3600
            header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
            claims = b64u(json.dumps({"aud": audience, "exp": exp, "sub": self.subject}, separators=(",", ":")).encode())
            signing_input = f"{header}.{claims}".encode()
            r, s = decode_dss_signature(self.key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
            token = f"{header}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
            cached = (float(exp), token)
            self._cache[audience] = cached
        return f"vapid t={cached[1]}, k={self.public}"


class PushError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Push:
    def __init__(self, cfg: Config, badge=lambda: None, valid=lambda cred_id: True):
        self.cfg = cfg
        self.badge = badge  # () -> int | None: agents waiting, for the app icon
        self.valid = valid  # cred_id -> bool: a removed passkey's devices get nothing
        self.vapid = Vapid(cfg.state_dir / "vapid.pem", cfg.push_subject or f"https://{cfg.host}")
        self.file = cfg.state_dir / "push.json"
        self.subs: list[dict] = self._load()
        self.present: dict[str, float] = {}  # device -> last "visible" report
        self.recent: dict[str, float] = {}  # dedupe tag -> sent at
        self.http: aiohttp.ClientSession | None = None
        self.allow_hosts = PUSH_HOSTS
        self.allow_http = False  # tests only

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.file.read_text())
            return [s for s in data if isinstance(s, dict) and "endpoint" in s]
        except (FileNotFoundError, ValueError):
            return []

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.subs, f, indent=1)
        os.replace(tmp, self.file)

    # -- subscriptions ---------------------------------------------------------

    def allowed(self, endpoint: str) -> bool:
        parts = urlsplit(endpoint)
        if parts.scheme != "https" and not (self.allow_http and parts.scheme == "http"):
            return False
        host = (parts.hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.allow_hosts)

    def subscribe(self, sub: dict, device: str, cred_id: str, kinds: list[str]) -> None:
        endpoint = sub.get("endpoint")
        keys = sub.get("keys") or {}
        if not isinstance(endpoint, str) or len(endpoint) > 2048 or not self.allowed(endpoint):
            raise PushError("unsupported push service")
        try:
            p256dh, auth = unb64u(keys["p256dh"]), unb64u(keys["auth"])
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
        except (KeyError, TypeError, ValueError) as e:
            raise PushError("bad subscription keys") from e
        if len(auth) < 16:
            raise PushError("bad subscription keys")
        entry = {
            "endpoint": endpoint,
            "p256dh": b64u(p256dh),
            "auth": b64u(auth),
            "device": device[:64],
            "cred": cred_id,
            "kinds": [k for k in kinds if k in KINDS],
            "created": int(time.time()),
        }
        # One subscription per device: a re-subscribe replaces the old one.
        self.subs = [s for s in self.subs if s["endpoint"] != endpoint and s.get("device") != entry["device"]]
        self.subs.append(entry)
        self.subs = self.subs[-MAX_SUBSCRIPTIONS:]
        self._save()
        log.info("push: device subscribed (%d total)", len(self.subs))

    def unsubscribe(self, device: str) -> None:
        before = len(self.subs)
        self.subs = [s for s in self.subs if s.get("device") != device]
        if len(self.subs) != before:
            self._save()

    def device(self, device: str) -> dict | None:
        return next((s for s in self.subs if s.get("device") == device), None)

    def seen(self, device: str, visible: bool) -> None:
        if visible:
            self.present[device] = time.monotonic()
        else:
            self.present.pop(device, None)

    def _in_front(self, device: str) -> bool:
        at = self.present.get(device)
        return at is not None and time.monotonic() - at < PRESENT_FOR

    # -- sending ---------------------------------------------------------------

    def notify(self, item: dict) -> None:
        """Hub alert listener: push it to every device that wants this kind and isn't looking."""
        kind = item.get("kind") or "info"
        where = f"{item['machine']}:" if item.get("machine") else ""  # the same project on two machines is two alerts
        tag = f"{kind}:{where}{item.get('project') or item.get('agent') or item.get('title')}"
        if time.time() - (item.get("time") or time.time()) > STALE_AFTER:
            log.info("push: %s not sent: an old alert", tag)  # a replayed backlog (ntfy sends the last hour on reconnect)
            return
        now = time.monotonic()
        self.recent = {t: at for t, at in self.recent.items() if now - at < DEDUPE_FOR}
        if tag in self.recent:
            log.info("push: %s not sent: the same alert just went out", tag)
            return
        wanting = [s for s in self.subs if kind in s.get("kinds", KINDS) and self.valid(s.get("cred", ""))]
        targets = [s for s in wanting if not self._in_front(s.get("device", ""))]
        if not targets:
            why = "the app is open on the phone" if wanting else "no device wants this kind"
            log.info("push: %s not sent: %s", tag, why)
            return
        self.recent[tag] = now
        log.info("push: %s sent to %d device(s)", tag, len(targets))
        payload = {
            "title": item.get("pushTitle") or item.get("title") or "lariat",
            "body": item.get("pushBody") or item.get("message") or "",
            # Each alert its own notification: one with the same tag replaces an
            # earlier one silently on iOS (no banner, no sound).
            "tag": f"{tag}:{item.get('id') or int(time.time() * 1000)}",
            "url": item.get("url") or "/",
            "badge": self.badge(),
        }
        for sub in targets:
            asyncio.create_task(self._send(sub, payload, urgency="high" if kind == "blocked" else "normal"))

    async def test(self, device: str) -> None:
        sub = self.device(device)
        if sub is None or not self.valid(sub.get("cred", "")):
            raise PushError("this device is not subscribed", 404)
        ok = await self._send(sub, {"title": self.cfg.app_name, "body": "Notifications work.", "tag": "test", "url": "/"})
        if not ok:
            raise PushError("the push service refused it", 502)

    async def _send(self, sub: dict, payload: dict, urgency: str = "normal") -> bool:
        if self.http is None:
            self.http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls.public_context()))
        body = json.dumps(payload, separators=(",", ":")).encode()
        try:
            data = encrypt(body, unb64u(sub["p256dh"]), unb64u(sub["auth"]))
            headers = {
                "Authorization": self.vapid.authorization(sub["endpoint"]),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(TTL),
                "Urgency": urgency,
            }
            async with self.http.post(sub["endpoint"], data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                status = r.status
                reason = (await r.text())[:200] if status >= 400 else ""
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.warning("push: send failed: %s", type(e).__name__)
            return False
        if status in (404, 410):  # the device dropped the subscription
            log.info("push: subscription gone (%d), removing it", status)
            self.subs = [s for s in self.subs if s["endpoint"] != sub["endpoint"]]
            self._save()
            return False
        if status >= 300:
            log.warning("push: push service answered %d %s", status, reason)
            return False
        return True

    async def close(self) -> None:
        if self.http:
            await self.http.close()
            self.http = None
