"""Your own certificate authority for one host.

For when the phone should trust lariat without a public certificate, so the
host name never appears in Certificate Transparency logs. `lariat tls init`
makes a CA that can only sign this one name (X.509 name constraints, so the
phone trusting it risks nothing else) and a server certificate signed by it;
`lariat tls phone` serves the CA certificate on the tailnet for the phone to
install. Keys stay in the state dir (0600).
"""

from __future__ import annotations

import datetime as dt
import os
import ssl
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_DAYS = 3650
LEAF_DAYS = 800  # Apple platforms reject TLS server certificates valid for more than 825 days


def public_context() -> ssl.SSLContext:
    """TLS for public servers (GitHub, push services, ts.net names). The
    release binaries also carry Mozilla's CA list (certifi): the OpenSSL inside
    them looks for the system's CAs where the build machine kept them."""
    ctx = ssl.create_default_context()
    if getattr(sys, "frozen", False):
        try:
            import certifi

            ctx.load_verify_locations(cafile=certifi.where())
        except (ImportError, OSError):
            pass
    return ctx


def paths(state: Path) -> dict[str, Path]:
    d = state / "tls"
    return {"dir": d, "ca": d / "ca.pem", "ca_key": d / "ca.key", "cert": d / "cert.pem", "key": d / "key.pem"}


def _write(path: Path, data: bytes, private: bool) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def make_ca(host: str, now: dt.datetime) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"lariat CA ({host})")])
    public = key.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        # The CA can vouch for this one name and nothing else.
        .add_extension(x509.NameConstraints(permitted_subtrees=[x509.DNSName(host)], excluded_subtrees=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def make_leaf(host: str, ca_key: ec.EllipticCurvePrivateKey, ca: x509.Certificate, now: dt.datetime):
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca.subject)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
                ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


AUTO = "made-automatically"  # in the tls dir: `lariat serve` made the CA (no Tailscale certificates then)


def made_automatically(state: Path) -> bool:
    return (paths(state)["dir"] / AUTO).exists()


def keep(state: Path) -> None:
    """The CA is the user's choice (`lariat tls init`): used even with Tailscale's certificates on."""
    (paths(state)["dir"] / AUTO).unlink(missing_ok=True)


def init(state: Path, host: str, renew: bool = False, automatic: bool = False) -> dict[str, Path]:
    """Create the CA (once) and a server certificate for `host` (again with renew).
    automatic: made by `lariat serve` because the tailnet has no certificates;
    it gives way to Tailscale's certificate once those are turned on."""
    p = paths(state)
    p["dir"].mkdir(parents=True, exist_ok=True, mode=0o700)
    if automatic and not p["ca"].exists():
        (p["dir"] / AUTO).write_text("lariat serve made this CA: the tailnet had no HTTPS certificates\n")
    now = dt.datetime.now(dt.timezone.utc)
    if p["ca"].exists():
        ca = x509.load_pem_x509_certificate(p["ca"].read_bytes())
        constraints = ca.extensions.get_extension_for_class(x509.NameConstraints).value.permitted_subtrees or []
        if [n.value for n in constraints] != [host]:
            raise ValueError(f"the CA in {p['dir']} is for {constraints[0].value if constraints else '?'}, not {host}")
        if not p["ca_key"].exists():
            raise ValueError(f"the CA key was removed from {p['dir']}; restore it or delete the directory to start over")
        ca_key = serialization.load_pem_private_key(p["ca_key"].read_bytes(), None)
    else:
        ca_key, ca = make_ca(host, now)
        _write(p["ca_key"], _key_pem(ca_key), private=True)
        _write(p["ca"], ca.public_bytes(serialization.Encoding.PEM), private=False)
    if renew or not p["cert"].exists():
        key, cert = make_leaf(host, ca_key, ca, now)
        _write(p["key"], _key_pem(key), private=True)
        _write(p["cert"], cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM), private=False)
    return p


def ca_der(state: Path) -> bytes:
    ca = x509.load_pem_x509_certificate(paths(state)["ca"].read_bytes())
    return ca.public_bytes(serialization.Encoding.DER)


def days_left(cert_file: Path) -> int:
    cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
    return (cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)).days


PHONE_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Install the lariat certificate</title>
<style>body{{font:17px/1.45 -apple-system,system-ui,sans-serif;margin:24px;max-width:34em;background:#111412;color:#e8ece0}}
a.btn{{display:block;margin:20px 0;padding:16px;border-radius:12px;background:#a6c47c;color:#111412;text-align:center;font-weight:700;text-decoration:none}}
li{{margin:6px 0}}code{{color:#a6c47c}}</style>
<h1>Trust {host}</h1>
<p>This certificate lets your phone trust <code>{host}</code>, and only that name.</p>
<a class="btn" href="/lariat-ca.cer">Download the certificate</a>
<h3>iPhone / iPad</h3>
<ol><li>Tap the button above, then <b>Allow</b>.</li>
<li>Settings &rarr; <b>Profile Downloaded</b> &rarr; Install.</li>
<li>Settings &rarr; General &rarr; About &rarr; <b>Certificate Trust Settings</b> &rarr; turn on <b>lariat CA ({host})</b>.</li></ol>
<h3>Android</h3>
<ol><li>Tap the button above to save the file.</li>
<li>Settings &rarr; Security &rarr; Encryption &amp; credentials &rarr; <b>Install a certificate</b> &rarr; CA certificate &rarr; pick the file.</li></ol>
<p>Then open <a href="https://{origin_host}/" style="color:#a6c47c">https://{origin_host}/</a>.</p>
"""
