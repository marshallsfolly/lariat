import asyncio
import json
import os
import unittest

from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from lariat.app import Gateway
from lariat.providers.herdr import transitions
from lariat.push import b64u, encrypt, raw_public, unb64u

from .helpers import HOST, ORIGIN, TempDir, make_config
from .softauthn import SoftAuthenticator
from .test_app import POST, TOKEN

DEVICE = "device-0123456789"


def decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth_secret: bytes) -> bytes:
    """What the phone does with a push (RFC 8291, receiving side)."""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public, record = body[21 : 21 + idlen], body[21 + idlen :]
    assert rs >= len(record)
    as_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    ua_public = raw_public(ua_private.public_key())
    ecdh = ua_private.exchange(ec.ECDH(), as_key)
    ikm = HKDF(hashes.SHA256(), 32, salt=auth_secret, info=b"WebPush: info\x00" + ua_public + as_public).derive(ecdh)
    cek = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, record, None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


class EncryptionTest(unittest.TestCase):
    def test_rfc8291_example(self):
        # RFC 8291, section 5 and appendix A.
        as_private = ec.derive_private_key(
            int.from_bytes(unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1()
        )
        out = encrypt(
            unb64u("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
            unb64u("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcx aOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
            unb64u("BTBZMqHH6r4Tts7J_aSIgg"),
            salt=unb64u("DGv6ra1nlYgDCS1FRnbzlw"),
            as_private=as_private,
        )
        header = unb64u(
            "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z 9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml mlMoZIIgDll6e3vCYLocInmYWAmS6Tlz AC8wEqKK6PBru3jl7A8"
        )
        ciphertext = unb64u("8pfeW0KbunFT06SuDKoJH9Ql87S1QUrd irN6GcG7sFz1y1sqLgVi1VhjVkHsUoEs bI_0LpXMuGvnzQ")
        self.assertEqual(out, header + ciphertext)


class TransitionsTest(unittest.TestCase):
    def test_only_changes_to_blocked_or_done(self):
        pane = {"agent": "claude", "name": "api", "cwd": "shop", "title": "Allow edit?", "state_change_seq": 7}
        before = {"w1:p1": {**pane, "status": "working"}, "w1:p2": {**pane, "status": "blocked"}}
        after = {
            "w1:p1": {**pane, "status": "blocked"},
            "w1:p2": {**pane, "status": "blocked"},  # unchanged
            "w1:p3": {**pane, "status": "done"},  # new pane: no history, no alert
        }
        alerts = transitions("default", before, after)
        self.assertEqual([a["pane"] for a in alerts], ["w1:p1"])
        self.assertEqual(alerts[0]["pushTitle"], "api needs you")
        self.assertEqual(alerts[0]["url"], "/#go=agent:default:w1:p1")
        self.assertEqual(transitions("default", {}, after), [])  # first snapshot


class PushTest(TempDir, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        static = self.tmp / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>t</title>")
        self.cfg = make_config(self.tmp, static_dir=static)
        self.gateway = Gateway(self.cfg)
        server = TestServer(self.gateway.app())
        await server.start_server()
        jar = CookieJar(unsafe=True, treat_as_secure_origin=[str(server.make_url("/")).rstrip("/")])
        self.client = TestClient(server, cookie_jar=jar)
        await self.client.start_server()

        # A stand-in push service that records what it gets.
        self.received: list[tuple[dict, bytes]] = []
        self.status = 201

        async def endpoint(request: web.Request) -> web.Response:
            self.received.append((dict(request.headers), await request.read()))
            return web.Response(status=self.status)

        service = web.Application()
        service.router.add_post("/push/{id}", endpoint)
        self.service = TestServer(service)
        await self.service.start_server()
        push = self.gateway.push
        push.allow_hosts = ("127.0.0.1",)
        push.allow_http = True

        self.ua_private = ec.generate_private_key(ec.SECP256R1())
        self.auth_secret = os.urandom(16)

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.close()

    async def login(self):
        key = SoftAuthenticator(HOST, ORIGIN)
        r = await self.client.post("/auth/register/options", headers={**POST, "X-Enroll-Token": TOKEN})
        r = await self.client.post(
            "/auth/register/verify",
            headers={**POST, "X-Enroll-Token": TOKEN},
            json={"credential": key.create(await r.text()), "label": "test"},
        )
        self.assertEqual(r.status, 200)

    def subscription(self, endpoint=None):
        return {
            "endpoint": endpoint or str(self.service.make_url("/push/abc")),
            "keys": {"p256dh": b64u(raw_public(self.ua_private.public_key())), "auth": b64u(self.auth_secret)},
        }

    async def subscribe(self, **extra):
        body = {"subscription": self.subscription(), "device": DEVICE, "kinds": ["blocked", "done"], **extra}
        return await self.client.post("/api/push/subscribe", headers=POST, json=body)

    async def wait_received(self, n: int):
        async with asyncio.timeout(5):
            while len(self.received) < n:
                await asyncio.sleep(0.02)

    async def test_alert_arrives_encrypted_and_signed(self):
        await self.login()
        r = await self.client.get("/api/state")
        key = (await r.json())["config"]["push"]["key"]
        self.assertEqual((await self.subscribe()).status, 200)

        self.gateway.hub.alert({"kind": "blocked", "project": "shop", "pushTitle": "api needs you", "pushBody": "shop", "url": "/#go=x"})
        await self.wait_received(1)
        headers, body = self.received[0]
        self.assertEqual(headers["Content-Encoding"], "aes128gcm")
        self.assertEqual(headers["Urgency"], "high")
        payload = json.loads(decrypt(body, self.ua_private, self.auth_secret))
        self.assertEqual(payload["title"], "api needs you")
        self.assertEqual(payload["url"], "/#go=x")
        self.assertTrue(payload["tag"].startswith("blocked:shop:"))  # one notification per alert

        # VAPID: a JWT for the push service's origin, signed by the key the app subscribed with.
        scheme, rest = headers["Authorization"].split(" ", 1)
        self.assertEqual(scheme, "vapid")
        fields = dict(part.strip().split("=", 1) for part in rest.split(","))
        self.assertEqual(fields["k"], key)
        head, claims, sig = fields["t"].split(".")
        claims = json.loads(unb64u(claims))
        self.assertEqual(claims["aud"], str(self.service.make_url("/")).rstrip("/"))
        self.assertEqual(claims["sub"], f"https://{HOST}")
        public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(key))
        raw = unb64u(sig)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        public.verify(der, f"{head}.{fields['t'].split('.')[1]}".encode(), ec.ECDSA(hashes.SHA256()))

        # The same alert from a second source within the window is not pushed again.
        self.gateway.hub.alert({"kind": "blocked", "project": "shop", "title": "claude - shop"})
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.received), 1)

    async def test_backlog_is_not_pushed(self):
        await self.login()
        await self.subscribe()
        self.gateway.hub.alert({"kind": "blocked", "project": "old", "time": 1})
        await asyncio.sleep(0.2)
        self.assertEqual(self.received, [])

    async def test_no_push_while_the_app_is_in_front(self):
        await self.login()
        await self.subscribe()
        r = await self.client.post("/api/presence", headers=POST, json={"device": DEVICE, "visible": True})
        self.assertEqual(r.status, 200)
        self.gateway.hub.alert({"kind": "done", "project": "a"})
        await asyncio.sleep(0.2)
        self.assertEqual(self.received, [])
        await self.client.post("/api/presence", headers=POST, json={"device": DEVICE, "visible": False})
        self.gateway.hub.alert({"kind": "done", "project": "b"})
        await self.wait_received(1)

    async def test_kinds_test_and_gone(self):
        await self.login()
        await self.subscribe(kinds=["blocked"])
        self.gateway.hub.alert({"kind": "done", "project": "a"})  # not wanted
        await asyncio.sleep(0.2)
        self.assertEqual(self.received, [])
        r = await self.client.post("/api/push/test", headers=POST, json={"device": DEVICE})
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(decrypt(self.received[0][1], self.ua_private, self.auth_secret))["tag"], "test")
        self.status = 410  # the phone dropped it
        r = await self.client.post("/api/push/test", headers=POST, json={"device": DEVICE})
        self.assertEqual(r.status, 502)
        self.assertEqual(self.gateway.push.subs, [])

    async def test_refuses_other_endpoints_and_strangers(self):
        r = await self.subscribe()
        self.assertEqual(r.status, 401)
        await self.login()
        self.gateway.push.allow_http = False
        self.assertEqual((await self.subscribe()).status, 400)  # http
        bad = {"subscription": self.subscription("https://evil.test/x"), "device": DEVICE}
        r = await self.client.post("/api/push/subscribe", headers=POST, json=bad)
        self.assertEqual(r.status, 400)
        r = await self.client.post("/api/push/subscribe", headers={"Origin": ORIGIN}, json=bad)
        self.assertEqual(r.status, 403)  # no CSRF header
