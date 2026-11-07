"""A minimal software passkey (ES256, attestation "none") for tests."""

from __future__ import annotations

import base64
import hashlib
import json
import os

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class SoftAuthenticator:
    def __init__(self, rp_id: str, origin: str, counter: int | None = None):
        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.user_handle = b""
        # None = synced passkey behaviour (always 0); an int = incrementing counter.
        self.counter = counter

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def _count(self) -> int:
        if self.counter is None:
            return 0
        self.counter += 1
        return self.counter

    def create(self, options_json: str, origin: str | None = None, uv: bool = True) -> dict:
        opts = json.loads(options_json)
        self.user_handle = base64.urlsafe_b64decode(opts["user"]["id"] + "==")
        client_data = json.dumps(
            {"type": "webauthn.create", "challenge": opts["challenge"], "origin": origin or self.origin, "crossOrigin": False}
        ).encode()
        flags = UP | AT | BE | BS | (UV if uv else 0)
        attested = bytes(16) + len(self.cred_id).to_bytes(2, "big") + self.cred_id + self._cose()
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + self._count().to_bytes(4, "big") + attested
        att_obj = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64u(self.cred_id),
            "rawId": b64u(self.cred_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64u(client_data),
                "attestationObject": b64u(att_obj),
                "transports": ["internal", "hybrid"],
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }

    def get(self, options_json: str, origin: str | None = None, uv: bool = True, count: int | None = None) -> dict:
        opts = json.loads(options_json)
        client_data = json.dumps(
            {"type": "webauthn.get", "challenge": opts["challenge"], "origin": origin or self.origin, "crossOrigin": False}
        ).encode()
        flags = UP | BE | BS | (UV if uv else 0)
        sign_count = self._count() if count is None else count
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + sign_count.to_bytes(4, "big")
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64u(self.cred_id),
            "rawId": b64u(self.cred_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64u(client_data),
                "authenticatorData": b64u(auth_data),
                "signature": b64u(signature),
                "userHandle": b64u(self.user_handle),
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }
