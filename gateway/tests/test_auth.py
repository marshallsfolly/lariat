import json
import os
import unittest

from lariat.auth import Auth, AuthError, RateLimiter

from .helpers import HOST, ORIGIN, TempDir, make_config
from .softauthn import SoftAuthenticator

TOKEN = "tok-" + "x" * 28


class AuthTest(TempDir, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_config(self.tmp)
        self.auth = Auth(self.cfg)
        self.key = SoftAuthenticator(HOST, ORIGIN)

    def enroll(self, key=None):
        key = key or self.key
        pre, options = self.auth.register_options(TOKEN)
        opts = json.loads(options)
        self.assertEqual(opts["rp"]["id"], HOST)
        self.assertEqual(opts["authenticatorSelection"]["residentKey"], "required")
        self.assertEqual(opts["authenticatorSelection"]["userVerification"], "required")
        self.assertEqual(opts["attestation"], "none")
        return self.auth.register_verify(pre, TOKEN, key.create(options), "phone")

    def login(self, key=None, **kw):
        key = key or self.key
        pre, options = self.auth.login_options()
        self.assertEqual(json.loads(options)["allowCredentials"], [])
        return self.auth.login_verify(pre, key.get(options, **kw))

    def test_enroll_then_login(self):
        record = self.enroll()
        self.assertEqual(record["label"], "phone")
        self.assertEqual(self.login(), record["id"])
        enrolled = json.loads((self.cfg.state_dir / "enrolled.json").read_text())
        self.assertEqual(enrolled[0]["public_key"], record["public_key"])
        if os.name == "posix":
            self.assertEqual((self.cfg.state_dir / "enrolled.json").stat().st_mode & 0o777, 0o600)

    def test_token_is_single_use(self):
        self.enroll()
        self.assertFalse(self.auth.enrollment_open())
        with self.assertRaises(AuthError):
            self.auth.register_options(TOKEN)
        # Even a fresh process (new Auth) refuses the burnt token.
        with self.assertRaises(AuthError):
            Auth(self.cfg).register_options(TOKEN)

    def test_wrong_token(self):
        with self.assertRaises(AuthError) as cm:
            self.auth.register_options("tok-wrong-wrong-wrong-wrong")
        self.assertEqual(cm.exception.status, 403)

    def test_enrollment_closed(self):
        self.cfg.enrollment = False
        self.assertFalse(self.auth.enrollment_open())
        with self.assertRaises(AuthError) as cm:
            self.auth.register_options(TOKEN)
        self.assertEqual(cm.exception.status, 404)

    def test_enrolled_file_ignored_once_enrollment_closes(self):
        self.enroll()
        self.cfg.enrollment = False
        with self.assertRaises(AuthError):
            self.login()

    def test_declarative_credentials(self):
        record = self.enroll()
        self.cfg.credentials_file.write_text(json.dumps([record]))
        self.cfg.enrollment = False
        self.assertEqual(self.login(), record["id"])

    def test_wrong_origin_rejected(self):
        self.enroll()
        with self.assertRaises(AuthError):
            self.login(origin="https://evil.test")

    def test_user_verification_required(self):
        self.enroll()
        with self.assertRaises(AuthError):
            self.login(uv=False)

    def test_challenge_single_use(self):
        self.enroll()
        pre, options = self.auth.login_options()
        cred = self.key.get(options)
        self.auth.login_verify(pre, cred)
        with self.assertRaises(AuthError):
            self.auth.login_verify(pre, cred)

    def test_unknown_passkey(self):
        self.enroll()
        with self.assertRaises(AuthError) as cm:
            self.login(key=SoftAuthenticator(HOST, ORIGIN))
        self.assertEqual(cm.exception.status, 401)

    def test_zero_counter_always_accepted(self):
        self.enroll()
        for _ in range(3):
            self.login()

    def test_decreasing_counter_rejected(self):
        key = SoftAuthenticator(HOST, ORIGIN, counter=0)
        self.enroll(key)
        self.login(key)  # count 2
        self.login(key)  # count 3
        with self.assertRaises(AuthError):
            self.login(key, count=1)

    def test_expired_challenge(self):
        now = [1000.0]
        auth = Auth(self.cfg, clock=lambda: now[0])
        pre, options = auth.login_options()
        now[0] += 121
        with self.assertRaises(AuthError):
            auth.login_verify(pre, self.key.get(options))


class RateLimiterTest(unittest.TestCase):
    def test_per_remote_limit(self):
        now = [0.0]
        rl = RateLimiter(clock=lambda: now[0])
        for _ in range(10):
            rl.check("a")
        with self.assertRaises(AuthError):
            rl.check("a")
        rl.check("b")
        now[0] += 61
        rl.check("a")

    def test_global_lockout(self):
        now = [0.0]
        rl = RateLimiter(clock=lambda: now[0])
        for _ in range(20):
            rl.failure()
        with self.assertRaises(AuthError):
            rl.check("c")
        now[0] += 301
        rl.check("c")


if __name__ == "__main__":
    unittest.main()
