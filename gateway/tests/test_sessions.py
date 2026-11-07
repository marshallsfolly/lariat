import unittest

from lariat.sessions import SessionStore


class SessionStoreTest(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.store = SessionStore(b"k" * 32, idle_seconds=1800, absolute_seconds=8 * 3600, clock=lambda: self.now[0])

    def test_roundtrip(self):
        session, cookie = self.store.create("cred")
        self.assertIs(self.store.get(cookie), session)

    def test_forged_signature(self):
        _, cookie = self.store.create("cred")
        sid, _ = cookie.split(".")
        self.assertIsNone(self.store.get(sid + ".AAAA"))
        self.assertIsNone(self.store.get(sid))
        self.assertIsNone(self.store.get(None))

    def test_other_key_rejected(self):
        _, cookie = self.store.create("cred")
        other = SessionStore(b"j" * 32, 1800, 8 * 3600)
        self.assertIsNone(other.get(cookie))

    def test_idle_expiry_and_touch(self):
        session, cookie = self.store.create("cred")
        self.now[0] += 1700
        self.assertIsNotNone(self.store.get(cookie))  # touches
        self.now[0] += 1700
        self.assertIsNotNone(self.store.get(cookie))
        self.now[0] += 1801
        self.assertIsNone(self.store.get(cookie))
        self.assertFalse(self.store.alive(session))

    def test_absolute_expiry(self):
        _, cookie = self.store.create("cred")
        for _ in range(17):
            self.now[0] += 1700
            self.store.get(cookie)
        self.assertIsNone(self.store.get(cookie))

    def test_revoke(self):
        session, cookie = self.store.create("cred")
        self.store.revoke(session)
        self.assertIsNone(self.store.get(cookie))

    def test_alive_does_not_touch(self):
        session, _ = self.store.create("cred")
        self.now[0] += 1000
        self.assertTrue(self.store.alive(session))
        self.now[0] += 900
        self.assertFalse(self.store.alive(session))


if __name__ == "__main__":
    unittest.main()
