import asyncio
import datetime as dt
import ssl
import unittest

import aiohttp
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from lariat import cli, machines, tls

from .helpers import TempDir

HOST = "box.example.ts.net"


class TlsTest(TempDir, unittest.IsolatedAsyncioTestCase):
    def test_ca_signs_one_name(self):
        p = tls.init(self.tmp, HOST)
        ca = x509.load_pem_x509_certificate(p["ca"].read_bytes())
        leaf = x509.load_pem_x509_certificate(p["cert"].read_bytes())  # first PEM block
        constraints = ca.extensions.get_extension_for_class(x509.NameConstraints)
        self.assertTrue(constraints.critical)
        self.assertEqual([n.value for n in constraints.value.permitted_subtrees], [HOST])
        self.assertEqual(leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName), [HOST])
        ca.public_key().verify(leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(leaf.signature_hash_algorithm))
        days = (leaf.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)).days
        self.assertTrue(790 <= days <= 800)

    def test_renew_keeps_the_ca_and_refuses_another_host(self):
        p = tls.init(self.tmp, HOST)
        ca, cert = p["ca"].read_bytes(), p["cert"].read_bytes()
        tls.init(self.tmp, HOST)
        self.assertEqual(p["cert"].read_bytes(), cert)  # nothing to do
        tls.init(self.tmp, HOST, renew=True)
        self.assertEqual(p["ca"].read_bytes(), ca)
        self.assertNotEqual(p["cert"].read_bytes(), cert)
        with self.assertRaises(ValueError):
            tls.init(self.tmp, "other.example.ts.net")

    def test_phone_page_and_certificate(self):
        import threading
        import urllib.request

        p = tls.init(self.tmp, HOST)
        server = cli.phone_server(self.tmp, HOST, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(base + "/") as r:
                self.assertIn(f"https://{HOST}/", r.read().decode())
            with urllib.request.urlopen(base + "/lariat-ca.cer") as r:
                self.assertEqual(r.headers["Content-Type"], "application/x-x509-ca-cert")
                self.assertEqual(x509.load_der_x509_certificate(r.read()), x509.load_pem_x509_certificate(p["ca"].read_bytes()))
        finally:
            server.shutdown()
            server.server_close()

    def test_a_ca_made_for_want_of_certificates_gives_way(self):
        tls.init(self.tmp, HOST, automatic=True)  # `lariat serve` on a tailnet without certificates
        self.assertTrue(tls.made_automatically(self.tmp))
        self.assertTrue(cli.own_ca_in_use(self.tmp, certs_on=False))
        self.assertFalse(cli.own_ca_in_use(self.tmp, certs_on=True))  # certificates turned on: Tailscale's
        tls.keep(self.tmp)  # `lariat tls init`: the user's choice
        self.assertTrue(cli.own_ca_in_use(self.tmp, certs_on=True))

    def test_cli_init(self):
        cli.main(["tls", "init", "--host", HOST, "--state", str(self.tmp)])
        self.assertTrue((self.tmp / "tls" / "key.pem").exists())

    async def serve(self, cert_pem: bytes, key_pem: bytes) -> tuple[web.AppRunner, int]:
        (self.tmp / "srv.pem").write_bytes(cert_pem)
        (self.tmp / "srv.key").write_bytes(key_pem)
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.tmp / "srv.pem", self.tmp / "srv.key")
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text="ok"))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ctx)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        return runner, site._server.sockets[0].getsockname()[1]

    async def test_a_client_that_trusts_only_the_ca_connects(self):
        p = tls.init(self.tmp, HOST)
        _, port = await self.serve(p["cert"].read_bytes(), p["key"].read_bytes())
        client = ssl.create_default_context(cafile=str(p["ca"]))
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://127.0.0.1:{port}/", ssl=client, server_hostname=HOST) as r:
                self.assertEqual(await r.text(), "ok")

    async def test_the_ca_cannot_vouch_for_other_names(self):
        p = tls.init(self.tmp, HOST)
        # Even with the CA key, a certificate for another name fails the name constraint.
        ca = x509.load_pem_x509_certificate(p["ca"].read_bytes())
        ca_key = serialization.load_pem_private_key(p["ca_key"].read_bytes(), None)
        key, cert = tls.make_leaf("other.example.ts.net", ca_key, ca, dt.datetime.now(dt.timezone.utc))
        _, port = await self.serve(cert.public_bytes(serialization.Encoding.PEM), tls._key_pem(key))
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: None)  # the refused handshake
        client = ssl.create_default_context(cafile=str(p["ca"]))
        async with aiohttp.ClientSession() as s:
            with self.assertRaises(aiohttp.ClientConnectorCertificateError) as e:
                await s.get(f"https://127.0.0.1:{port}/", ssl=client, server_hostname="other.example.ts.net")
        self.assertIn("subtree", str(e.exception).lower())  # OpenSSL: "permitted subtree violation"

    async def test_a_hub_pins_a_link_code_certificate(self):
        p = tls.init(self.tmp, HOST)
        _, port = await self.serve(p["cert"].read_bytes(), p["key"].read_bytes())
        leaf = p["cert"].read_text().split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----\n"
        async with aiohttp.ClientSession() as s:
            for pinned in (p["ca"].read_text(), leaf):  # the CA, or (no CA at hand) the certificate itself
                async with s.get(f"https://127.0.0.1:{port}/", ssl=machines.ssl_context(pinned), server_hostname=HOST) as r:
                    self.assertEqual(await r.text(), "ok")
            asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: None)
            other = tls.init(self.tmp / "other", HOST)  # same name, another CA: refused
            with self.assertRaises(aiohttp.ClientConnectorCertificateError):
                await s.get(f"https://127.0.0.1:{port}/", ssl=machines.ssl_context(other["ca"].read_text()), server_hostname=HOST)

    async def test_link_and_add_over_https(self):
        import contextlib
        import io
        import os

        from lariat.app import Gateway

        from .helpers import make_config

        # A machine with its own CA, serving lariat over HTTPS as "localhost".
        node = make_config(self.tmp, static_dir=self.tmp, push=False)
        p = tls.init(node.state_dir, "localhost")
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(p["cert"], p["key"])
        runner = web.AppRunner(Gateway(node).app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ctx)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        port = site._server.sockets[0].getsockname()[1]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await asyncio.to_thread(cli.main, ["link", "--host", "localhost", "--https-port", str(port), "--state", str(node.state_dir)])
        code = next(w for w in out.getvalue().split() if w.startswith("lariat-link:"))
        hub_state = self.tmp / "hub"
        os.environ.pop("LARIAT_STATE_DIR", None)
        with contextlib.redirect_stdout(io.StringIO()) as added:
            await asyncio.to_thread(cli.main, ["machines", "add", code, "--state", str(hub_state)])
        self.assertIn("Added localhost", added.getvalue())  # it reached the machine, trusting only its CA
        entry = machines.Registry(hub_state / "machines.json").load()[0]
        self.assertEqual((entry["url"], entry["ca"]), (f"https://localhost:{port}", p["ca"].read_text()))
        with contextlib.redirect_stdout(io.StringIO()):
            await asyncio.to_thread(cli.main, ["link", "--revoke", "hub", "--state", str(node.state_dir)])
        with self.assertRaises(SystemExit) as e:
            await asyncio.to_thread(cli.main, ["machines", "add", code, "--state", str(hub_state)])
        self.assertIn("refused the link", str(e.exception))

    async def test_join_checks_the_hub_by_its_fingerprint(self):
        import contextlib
        import io

        from lariat.app import Gateway

        from .helpers import make_config

        async def serve_gateway(gateway, cert):
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(cert["cert"], cert["key"])
            runner = web.AppRunner(gateway.app(), shutdown_timeout=0.5)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ctx)
            await site.start()
            self.addAsyncCleanup(runner.cleanup)
            return site._server.sockets[0].getsockname()[1]

        (self.tmp / "hub").mkdir()
        (self.tmp / "node").mkdir()
        hub_cfg = make_config(self.tmp / "hub", static_dir=self.tmp, push=False)
        hub_tls = tls.init(hub_cfg.state_dir, "localhost")
        hub_cfg.cert_file = hub_tls["cert"]  # its own certificate: codes carry its fingerprint
        node_cfg = make_config(self.tmp / "node", static_dir=self.tmp, push=False)
        node_tls = tls.init(node_cfg.state_dir, "localhost")
        hub = Gateway(hub_cfg)
        hub_port = await serve_gateway(hub, hub_tls)
        node_port = await serve_gateway(Gateway(node_cfg), node_tls)
        pin = machines.pin_of_file(hub_tls["cert"])
        argv = ["join", f"localhost:{hub_port}", "", "--host", "localhost", "--https-port", str(node_port), "--port", str(node_port), "--state", str(node_cfg.state_dir)]
        argv[2] = machines.format_code(hub.invites.create() + "Z" * machines.PIN_LEN)  # a fingerprint that is not the hub's
        with self.assertRaises(SystemExit) as e:
            await asyncio.to_thread(cli.main, argv)
        self.assertIn("not the machine that made this code", str(e.exception))
        argv[2] = machines.format_code(hub.invites.create() + pin)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            await asyncio.to_thread(cli.main, argv)
        self.assertIn("Added localhost", out.getvalue())
        entry = hub.machines.registry.load()[0]
        self.assertEqual((entry["url"], entry["ca"]), (f"https://localhost:{node_port}", node_tls["ca"].read_text()))
