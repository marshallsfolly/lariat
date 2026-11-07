"""Passkey (WebAuthn) login and one-time enrollment, via py_webauthn.

Credentials come from the declarative credentials file (a JSON list, from
sops or any secrets manager). While enrollment is on, credentials enrolled
through the app are also accepted from <state>/enrolled.json until the owner
moves them into the declarative file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .config import Config, read_secret

log = logging.getLogger("lariat.auth")

CHALLENGE_TTL = 120
MAX_PENDING = 64


class AuthError(Exception):
    """A login or enrollment failure. The message is safe to show the client."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Pending:
    challenge: bytes
    kind: str  # "login" | "register"
    expires: float
    token_hash: str = ""
    user_id: bytes = b""


def _write_private(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.replace(tmp, path)


def _token_hash(token: bytes) -> str:
    return hashlib.sha256(token).hexdigest()


class RateLimiter:
    """10 attempts a minute per remote, and a global 5-minute lock after 20 failures."""

    def __init__(self, per_minute: int = 10, max_failures: int = 20, lockout: int = 300, clock=time.monotonic):
        self.per_minute = per_minute
        self.max_failures = max_failures
        self.lockout = lockout
        self._clock = clock
        self._hits: dict[str, list[float]] = {}
        self._failures: list[float] = []
        self._locked_until = 0.0

    def check(self, remote: str) -> None:
        now = self._clock()
        if now < self._locked_until:
            raise AuthError("too many failed attempts, try again later", 429)
        hits = [t for t in self._hits.get(remote, []) if now - t < 60]
        if len(hits) >= self.per_minute:
            self._hits[remote] = hits
            raise AuthError("slow down", 429)
        hits.append(now)
        self._hits[remote] = hits
        if len(self._hits) > 1024:
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < 60}

    def failure(self) -> None:
        now = self._clock()
        self._failures = [t for t in self._failures if now - t < 600]
        self._failures.append(now)
        if len(self._failures) >= self.max_failures:
            self._locked_until = now + self.lockout
            self._failures.clear()
            log.warning("auth: %d failures, locking logins for %ds", self.max_failures, self.lockout)


class Auth:
    def __init__(self, cfg: Config, clock=time.monotonic):
        self.cfg = cfg
        self._clock = clock
        self._pending: dict[str, Pending] = {}
        self.limiter = RateLimiter(clock=clock)
        self.enrolled_file = cfg.state_dir / "enrolled.json"
        self.consumed_file = cfg.state_dir / "token-consumed"
        self.counters_file = cfg.state_dir / "counters.json"

    # -- credentials -----------------------------------------------------

    def credentials(self) -> list[dict]:
        creds: list[dict] = []
        sources = [self.cfg.credentials_file]
        if self.cfg.enrollment:
            sources.append(self.enrolled_file)
        for path in sources:
            raw = read_secret(path)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.error("credentials file %s is not valid JSON; ignoring it", path)
                continue
            if isinstance(data, dict):
                data = [data]
            for item in data if isinstance(data, list) else []:
                if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("public_key"), str):
                    creds.append(item)
        return creds

    def known(self, cred_id: str) -> bool:
        return self._find(cred_id) is not None

    def _find(self, cred_id: str) -> dict | None:
        for cred in self.credentials():
            if hmac.compare_digest(cred["id"], cred_id):
                return cred
        return None

    def _counters(self) -> dict[str, int]:
        try:
            return json.loads(self.counters_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _set_counter(self, cred_id: str, count: int) -> None:
        if count <= 0:
            return
        counters = self._counters()
        counters[cred_id] = count
        _write_private(self.counters_file, json.dumps(counters))

    # -- challenges ------------------------------------------------------

    def _put(self, pending: Pending) -> str:
        now = self._clock()
        for key, p in list(self._pending.items()):
            if p.expires < now:
                del self._pending[key]
        while len(self._pending) >= MAX_PENDING:
            self._pending.pop(next(iter(self._pending)))
        pre = bytes_to_base64url(secrets.token_bytes(24))
        self._pending[pre] = pending
        return pre

    def _take(self, pre: str | None, kind: str) -> Pending:
        pending = self._pending.pop(pre or "", None)
        if pending is None or pending.kind != kind or pending.expires < self._clock():
            raise AuthError("challenge expired, try again")
        return pending

    # -- login -----------------------------------------------------------

    def login_options(self) -> tuple[str, str]:
        challenge = secrets.token_bytes(32)
        options = generate_authentication_options(
            rp_id=self.cfg.host,
            challenge=challenge,
            timeout=CHALLENGE_TTL * 1000,
            allow_credentials=[],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        pre = self._put(Pending(challenge=challenge, kind="login", expires=self._clock() + CHALLENGE_TTL))
        return pre, options_to_json(options)

    def login_verify(self, pre: str | None, credential: dict) -> str:
        pending = self._take(pre, "login")
        cred_id = credential.get("id") if isinstance(credential, dict) else None
        if not isinstance(cred_id, str):
            raise AuthError("malformed credential")
        stored = self._find(cred_id)
        if stored is None:
            raise AuthError("unknown passkey", 401)
        current = max(int(stored.get("sign_count", 0) or 0), self._counters().get(cred_id, 0))
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=pending.challenge,
                expected_rp_id=self.cfg.host,
                expected_origin=self.cfg.origin,
                credential_public_key=base64url_to_bytes(stored["public_key"]),
                credential_current_sign_count=current,
                require_user_verification=True,
            )
        except WebAuthnException as e:
            log.warning("auth: login verification failed: %s", e)
            raise AuthError("passkey verification failed", 401) from e
        self._set_counter(cred_id, verified.new_sign_count)
        return cred_id

    # -- enrollment ------------------------------------------------------

    def enrollment_open(self) -> bool:
        if not self.cfg.enrollment:
            return False
        token = read_secret(self.cfg.enroll_token_file)
        return len(token) >= 16 and not self._consumed(_token_hash(token))

    def _consumed(self, token_hash: str) -> bool:
        try:
            lines = self.consumed_file.read_text().split()
        except FileNotFoundError:
            return False
        return any(hmac.compare_digest(token_hash, line) for line in lines)

    def _check_token(self, given: str | None) -> str:
        if not self.cfg.enrollment:
            raise AuthError("enrollment is closed", 404)
        expected = read_secret(self.cfg.enroll_token_file)
        if len(expected) < 16:
            raise AuthError("enrollment is not configured", 404)
        given_bytes = (given or "").strip().encode()
        # Compare digests so the comparison is constant-time regardless of length.
        if not hmac.compare_digest(_token_hash(given_bytes), _token_hash(expected)):
            raise AuthError("wrong enrollment token", 403)
        token_hash = _token_hash(expected)
        if self._consumed(token_hash):
            raise AuthError("this enrollment token was already used", 403)
        return token_hash

    def register_options(self, token: str | None) -> tuple[str, str]:
        token_hash = self._check_token(token)
        challenge = secrets.token_bytes(32)
        user_id = secrets.token_bytes(16)
        options = generate_registration_options(
            rp_id=self.cfg.host,
            rp_name=self.cfg.rp_name,
            user_id=user_id,
            user_name=self.cfg.user_name,
            user_display_name=self.cfg.user_name,
            challenge=challenge,
            timeout=CHALLENGE_TTL * 1000,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"])) for c in self.credentials()
            ],
            supported_pub_key_algs=[
                COSEAlgorithmIdentifier.ECDSA_SHA_256,
                COSEAlgorithmIdentifier.EDDSA,
                COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
            ],
        )
        pre = self._put(
            Pending(
                challenge=challenge,
                kind="register",
                expires=self._clock() + CHALLENGE_TTL,
                token_hash=token_hash,
                user_id=user_id,
            )
        )
        return pre, options_to_json(options)

    def register_verify(self, pre: str | None, token: str | None, credential: dict, label: str = "") -> dict:
        token_hash = self._check_token(token)
        pending = self._take(pre, "register")
        if not hmac.compare_digest(pending.token_hash, token_hash):
            raise AuthError("challenge does not match this token")
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=pending.challenge,
                expected_rp_id=self.cfg.host,
                expected_origin=self.cfg.origin,
                require_user_verification=True,
            )
        except WebAuthnException as e:
            log.warning("auth: registration verification failed: %s", e)
            raise AuthError("passkey registration failed") from e

        transports = []
        response = credential.get("response") if isinstance(credential, dict) else None
        if isinstance(response, dict) and isinstance(response.get("transports"), list):
            transports = [t for t in response["transports"] if isinstance(t, str)][:8]
        record = {
            "id": bytes_to_base64url(verified.credential_id),
            "public_key": bytes_to_base64url(verified.credential_public_key),
            "sign_count": verified.sign_count,
            "transports": transports,
            "label": "".join(ch for ch in (label or "passkey") if ch.isprintable())[:32] or "passkey",
            "user_id": bytes_to_base64url(pending.user_id),
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        # Burn the token first: even if the write below fails, it never works twice.
        self.consumed_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.consumed_file, "a") as f:
            f.write(token_hash + "\n")
        try:
            existing = json.loads(self.enrolled_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            existing = []
        existing.append(record)
        _write_private(self.enrolled_file, json.dumps(existing, indent=2) + "\n")
        log.warning(
            "auth: enrolled a new passkey. Move it into your credentials secret, then turn enrollment off:\n%s",
            json.dumps(record),
        )
        return record
