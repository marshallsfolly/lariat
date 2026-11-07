"""`lariat`: run lariat on any machine on a tailnet (Linux, macOS, Windows).

    lariat setup        start here: Tailscale, HTTPS, the background service,
                         your phone and agent alerts, step by step
    lariat serve        find this node's MagicDNS name, put the gateway behind
                         `tailscale serve` and run it; the first time, show the
                         enrollment QR code
    lariat enroll       show a QR code to enroll a phone (a new one-time code
                         once the last one was used)
    lariat tls init     make your own CA and a certificate for this node, so
                         the name stays out of Certificate Transparency logs
    lariat tls phone    serve the CA certificate on the tailnet with a QR code,
                         for installing it on the phone
    lariat notify       send an alert to your phone (inbox and push); with
                         --hook, from a Claude Code or Grok hook
    lariat doctor       check everything and say what to fix (--json for agents)
    lariat hooks        report Claude Code / Grok sessions to lariat
    lariat service      install lariat as a background service (or remove it)
    lariat join         add this machine to your hub, with the code from the phone
    lariat link         let your hub (the machine your phone uses) reach this one
    lariat machines     on the hub: add, list or remove the machines it reaches
    lariat update       update to the latest release (or check, or set auto updates)

HTTPS comes from the node's ts.net certificate (`tailscale serve`), or, after
`lariat tls init`, from your own CA: the gateway then terminates TLS itself
and `tailscale serve` only forwards TCP. Secrets live in the state dir: a
session key, the one-time enrollment token, the passkeys enrolled with it and
the TLS keys. tmux, herdr and whisper-server are used when they are on PATH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from .config import ensure_secret, new_token

# Where Tailscale's CLI lives when it is not on PATH.
TAILSCALE_PATHS = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    r"C:\Program Files\Tailscale\tailscale.exe",
)


def state_dir(arg: str | None) -> Path:
    arg = arg or os.environ.get("LARIAT_STATE_DIR")
    if arg:
        return Path(arg).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "lariat"
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "lariat"


def find_tailscale() -> str | None:
    return shutil.which("tailscale") or next((p for p in TAILSCALE_PATHS if os.path.exists(p)), None)


def tailnet() -> dict:
    tailscale = find_tailscale()
    if not tailscale:
        raise SystemExit("lariat: Tailscale is not installed (or not on PATH); pass --host to run without it")
    try:
        out = subprocess.run([tailscale, "status", "--json"], capture_output=True, text=True, timeout=10, check=True).stdout
        status = json.loads(out)
    except (subprocess.SubprocessError, ValueError) as e:
        raise SystemExit(f"lariat: `tailscale status` failed ({e}); is Tailscale running and logged in?")
    me = status.get("Self") or {}
    name = (me.get("DNSName") or "").rstrip(".")
    if not name:
        raise SystemExit("lariat: this node has no MagicDNS name; turn MagicDNS on in the Tailscale admin console")
    return {"name": name, "https": bool(status.get("CertDomains")), "tailscale": tailscale, "ips": me.get("TailscaleIPs") or []}


def full_name(host: str) -> str:
    """A tailnet machine's MagicDNS name from its short name ("desktop" -> desktop.<tailnet>.ts.net)."""
    name, _, port = host.partition(":")
    tailscale = find_tailscale()
    if "." in name or not tailscale:
        return host
    try:
        status = json.loads(subprocess.run([tailscale, "status", "--json"], capture_output=True, text=True, timeout=10, check=True).stdout)
    except (subprocess.SubprocessError, ValueError, OSError):
        return host
    for node in [status.get("Self") or {}, *(status.get("Peer") or {}).values()]:
        dns = (node.get("DNSName") or "").rstrip(".")
        if dns.split(".")[0].lower() == name.lower():
            return dns + (f":{port}" if port else "")
    return host


def tailscale_serve(tailscale: str, args: list[str]) -> None:
    cmd = [tailscale, "serve", "--bg", "--yes", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        return
    err = (r.stderr or r.stdout).strip()
    if "access denied" in err.lower() or "permission" in err.lower():
        from .install import command_line

        if sys.platform == "win32":
            raise SystemExit(
                "lariat: `tailscale serve` needs an administrator. Once, in an administrator PowerShell, run:\n"
                f"    & {command_line(cmd)}\n"
                "then start lariat with --no-tailscale-serve (the setting stays)."
            )
        raise SystemExit(
            "lariat: `tailscale serve` needs permission. Once, run:\n"
            "    sudo tailscale set --operator=$USER\n"
            "then start lariat again."
        )
    raise SystemExit(f"lariat: `tailscale serve` failed:\n{err}")


def tailscale_unserve(tailscale: str, flag: str) -> None:
    subprocess.run([tailscale, "serve", flag, "off"], capture_output=True, timeout=30)


def token_used(state: Path, token: bytes) -> bool:
    digest = hashlib.sha256(token).hexdigest()
    try:
        return digest in (state / "token-consumed").read_text().split()
    except FileNotFoundError:
        return False


def fresh_token(state: Path, renew: bool = False) -> str:
    path = state / "enroll-token"
    ensure_secret(path, new_token)
    token = path.read_bytes().strip()
    if renew or token_used(state, token) or len(token) < 16:
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(new_token())
        os.replace(tmp, path)
        token = path.read_bytes().strip()
    return token.decode()


def enrolled(state: Path) -> int:
    try:
        data = json.loads((state / "enrolled.json").read_text())
        return len(data) if isinstance(data, list) else 0
    except (FileNotFoundError, ValueError):
        return 0


def show_qr(url: str, what: str) -> None:
    """A QR code in the terminal (half blocks; plain ANSI blocks where the console
    cannot encode them), then the link. Never fails: the link is enough."""
    try:
        import segno

        qr = segno.make(url, error="m")
        try:
            qr.terminal(compact=True, border=2)
        except UnicodeEncodeError:
            qr.terminal(border=2)
    except Exception:
        pass
    print(f"{what}\n{url}")


def enroll_url(host: str, token: str, https_port: int = 443) -> str:
    port = "" if https_port == 443 else f":{https_port}"
    return f"https://{host}{port}/enroll#t={quote(token, safe='')}"


# -- commands -------------------------------------------------------------------

ENROLL_HINT = "Scan it with your phone's camera, open the link, tap Create passkey. The code works once."


def cmd_enroll(args) -> None:
    state = state_dir(args.state)
    host = args.host or os.environ.get("LARIAT_HOST") or tailnet()["name"]
    token_file = os.environ.get("LARIAT_ENROLL_TOKEN_FILE")
    if token_file:  # a token from a secrets manager: shown, never rewritten
        token = Path(token_file).read_text().strip()
        info = gateway_info(state, args.port)
        if info is not None and not info.get("enroll"):
            raise SystemExit("lariat: enrollment is closed: that token was used. Rotate it in your secrets and redeploy.")
    else:
        token = fresh_token(state, renew=args.new)
    show_qr(enroll_url(host, token, args.https_port), ENROLL_HINT)


def cmd_tls_init(args) -> None:
    from . import tls

    state = state_dir(args.state)
    host = args.host or tailnet()["name"]
    try:
        p = tls.init(state, host, renew=args.renew)
    except ValueError as e:
        raise SystemExit(f"lariat: {e}")
    tls.keep(state)  # chosen: used even if Tailscale's certificates are turned on later
    print(f"CA and certificate for {host} in {p['dir']} (the certificate is good for {tls.days_left(p['cert'])} days).")
    print("Next: `lariat tls phone` to install the CA on your phone, then `lariat serve` uses it.")


def phone_server(state: Path, host: str, ip: str, port: int, https_port: int = 443):
    """An HTTP server for the phone: a page with the steps, and the CA certificate."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from . import tls

    der = tls.ca_der(state)
    page = tls.PHONE_PAGE.format(host=host, origin_host=host if https_port == 443 else f"{host}:{https_port}").encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server naming)
            if self.path.startswith("/lariat-ca.cer"):
                body, ctype = der, "application/x-x509-ca-cert"
            elif self.path in ("/", "/index.html"):
                body, ctype = page, "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return ThreadingHTTPServer((ip, port), Handler)


def cmd_tls_phone(args) -> None:
    """Serve the CA certificate over plain HTTP on the tailnet address only, until Ctrl-C."""
    from . import tls

    state = state_dir(args.state)
    if not tls.paths(state)["ca"].exists():
        raise SystemExit("lariat: no CA yet; run `lariat tls init` first")
    info = tailnet()
    host = args.host or info["name"]
    ip = next((a for a in info["ips"] if ":" not in a), None)
    if not ip:
        raise SystemExit("lariat: this node has no Tailscale IPv4 address")
    server = phone_server(state, host, ip, args.port, args.https_port)  # the tailnet address only, never 0.0.0.0
    show_qr(f"http://{host}:{args.port}/", "Scan it with your phone's camera and follow the steps on the page. Ctrl-C when done.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def notify_body(args, stdin: str = "") -> dict:
    body = {"kind": args.kind, "agent": args.agent, "project": args.project, "title": args.title, "message": " ".join(args.message)}
    if args.hook:
        agent, event = args.hook
        try:
            data = json.loads(stdin) if stdin.strip() else {}
        except ValueError:
            data = {}
        cwd = str(data.get("cwd") or os.getcwd())
        body.update(agent=agent, project=os.path.basename(cwd.rstrip("/\\")) or None)
        if event == "notification":
            body.update(kind="blocked", message=str(data.get("message") or "needs your input")[:300])
        else:
            body.update(kind="done", message=" ".join(str(data.get("last_assistant_message") or "finished").split())[:200])
    return {k: v for k, v in body.items() if v}


def local_request(state: Path, port: int, path: str, body: dict | None = None, headers: dict | None = None) -> bytes:
    """A request to this machine's gateway: HTTPS with our own CA if there is one, else HTTP."""
    from . import tls

    own = tls.paths(state)
    attempts = []
    if own["cert"].exists():
        ctx = ssl.create_default_context(cafile=str(own["ca"]))
        ctx.check_hostname = False  # loopback; the CA check still applies
        attempts.append((f"https://127.0.0.1:{port}", ctx))
    attempts.append((f"http://127.0.0.1:{port}", None))
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json", **(headers or {})} if body is not None else dict(headers or {})
    error: Exception | None = None
    for base, ctx in attempts:
        try:
            req = urllib.request.Request(base + path, data=data, headers=hdrs, method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=3, context=ctx) as r:
                return r.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError) as e:
            error = e
    raise error or OSError("gateway not reachable")


def post_notify(state: Path, port: int, body: dict) -> None:
    token = (state / "notify-token").read_text().strip()
    local_request(state, port, "/api/notify", body, {"X-Lariat-Notify": token})


def gateway_info(state: Path, port: int) -> dict | None:
    try:
        return json.loads(local_request(state, port, "/auth/info"))
    except (OSError, ValueError, urllib.error.HTTPError):
        return None


def cmd_notify(args) -> None:
    stdin = sys.stdin.read() if args.hook and not sys.stdin.isatty() else ""
    try:
        post_notify(state_dir(args.state), args.port, notify_body(args, stdin))
    except (OSError, ValueError) as e:
        if args.hook:
            return  # a hook must never fail or slow the agent
        raise SystemExit(f"lariat: could not reach the gateway on port {args.port}: {e}")


def cmd_doctor(args) -> None:
    from . import doctor

    raise SystemExit(doctor.report(doctor.run(state_dir(args.state), args.port, args.https_port), args.json))


def cmd_hooks(args) -> None:
    from . import install

    cmd = [args.command] if args.command else None
    if args.agent == "claude":
        if not args.install:
            print(json.dumps({"hooks": install.claude_hooks(cmd)}, indent=2))
            print(f"(add to {install.claude_settings()}, or run `lariat hooks claude --install`)", file=sys.stderr)
            return
        path = Path(args.settings).expanduser() if args.settings else None
        changed = install.install_claude_hooks(path, cmd, uploads=state_dir(None) / "uploads")
        where = path or install.claude_settings()
        print(f"Claude Code hooks {'added to' if changed else 'already in'} {where}. New Claude sessions report to lariat.")
    else:
        if not args.install:
            print(json.dumps({"hooks": {"Stop": [{"hooks": [install.hook("grok", "stop", cmd)]}]}}, indent=2))
            return
        print(f"Grok hooks written to {install.install_grok_hooks(cmd=cmd)}.")


def cmd_service(args) -> None:
    from . import install

    # serve_args takes everything after the action, so `--dry-run` may land there
    # too: it is ours anywhere before a `--`; the rest are options for `serve`.
    rest = list(args.serve_args)
    cut = rest.index("--") if "--" in rest else len(rest)
    ours, serve = rest[:cut], rest[cut + 1 :]
    dry_run = args.dry_run or "--dry-run" in ours
    extra = [a for a in ours if a != "--dry-run"] + serve
    if args.action == "install":
        install.install_service(extra, dry_run=dry_run)
    else:
        install.uninstall_service(dry_run=dry_run)


def cmd_link(args) -> None:
    import time

    from . import links, tls

    state = state_dir(args.state)
    store = links.Links(state / "links.json")
    if args.list:
        for d in store.load():
            print(f"{d['name']}  (since {time.strftime('%Y-%m-%d', time.localtime(d['created']))})")
        return
    if args.revoke:
        print(f"Revoked {args.revoke}." if store.revoke(args.revoke) else f"No link named {args.revoke}.")
        return
    host, url, ca, note = link_details(args, state)
    code = links.encode_code(url, store.create(args.name), host.split(".")[0], ca)
    print("On your hub (the machine your phone uses), run:\n")
    print(f"    lariat machines add {code}\n")
    print(f"That gives the hub your shells and agents here, like the phone has. Undo: `lariat link --revoke {args.name}`.{note}")


def own_ca_in_use(state: Path, certs_on: bool | None = None) -> bool:
    """Does this machine serve HTTPS with its own CA? (One `lariat serve` made
    only because the tailnet had no certificates gives way once they are on.)"""
    from . import tls

    if not tls.paths(state)["cert"].exists():
        return False
    if not tls.made_automatically(state):
        return True
    if certs_on is None:
        try:
            certs_on = tailnet()["https"]
        except SystemExit:
            certs_on = False
    return not certs_on


def link_details(args, state: Path) -> tuple[str, str, str | None, str]:
    """This machine as a hub reaches it: host, URL, what to trust, a note."""
    from . import tls

    host = args.host or os.environ.get("LARIAT_HOST") or tailnet()["name"]
    url = f"https://{host}" + ("" if args.https_port == 443 else f":{args.https_port}")
    # What the hub must trust to reach us: our CA, else (a certificate of our
    # own that no CA vouches for publicly) the certificate itself, else nothing
    # extra (Tailscale's certificate is publicly trusted).
    ca_file = args.ca or os.environ.get("LARIAT_LINK_CA")
    cert_file = os.environ.get("LARIAT_CERT_FILE")
    note = ""
    if ca_file:
        ca = Path(ca_file).expanduser().read_text()
    elif own_ca_in_use(state):
        ca = tls.paths(state)["ca"].read_text()
    elif cert_file and not publicly_trusted(url):
        ca = Path(cert_file).read_text()
        note = "\nThe hub pins this machine's certificate: after renewing it, link again (or pass --ca with the CA that signs it)."
    else:
        ca = None
    return host, url, ca, note


def hub_context(base: str, pin: str | None) -> ssl.SSLContext:
    """TLS to the hub: publicly trusted, or (its own certificate) the one whose
    fingerprint is in the join code."""
    from . import machines, tls

    if not pin:
        return tls.public_context()
    import socket
    from urllib.parse import urlsplit

    where = urlsplit(base)
    probe = ssl.create_default_context()
    probe.check_hostname = False
    probe.verify_mode = ssl.CERT_NONE  # only to read the certificate, checked against the pin next
    try:
        with socket.create_connection((where.hostname, where.port or 443), timeout=10) as sock:
            with probe.wrap_socket(sock, server_hostname=where.hostname) as conn:
                der = conn.getpeercert(binary_form=True)
    except OSError as e:
        raise SystemExit(f"lariat: cannot reach {base} ({e}): is that your hub's name, and Tailscale on here?")
    if machines.pin_of_der(der) != pin:
        raise SystemExit(f"lariat: {base} is not the machine that made this code (its certificate differs). Check the name.")
    ctx = ssl.create_default_context(cadata=ssl.DER_cert_to_PEM_cert(der))
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.check_hostname = False  # the fingerprint identifies it
    return ctx


def cmd_join(args) -> None:
    from urllib.parse import urlsplit

    from . import links, machines

    state = state_dir(args.state)
    code = machines.normalize_code(args.code)
    if len(code) not in (machines.TOKEN_LEN, machines.TOKEN_LEN + machines.PIN_LEN):
        raise SystemExit("lariat: that code looks incomplete; copy it again from the phone")
    pin = code[machines.TOKEN_LEN :] or None
    base = args.hub.rstrip("/") if args.hub.startswith(("https://", "http://")) else "https://" + full_name(args.hub)
    if gateway_info(state, args.port) is None:
        raise SystemExit("lariat: lariat is not running here; start it first (`lariat service install`), then join")
    host, url, ca, _ = link_details(args, state)
    name = machines.check_name(args.name or host.split(".")[0])
    hub = (urlsplit(base).hostname or "hub").split(".")[0]
    store = links.Links(state / "links.json")
    token = store.create(hub)
    ctx = hub_context(base, pin) if base.startswith("https://") else None
    body = json.dumps({"code": code, "name": name, "url": url, "token": token, "ca": ca}).encode()
    req = urllib.request.Request(base + "/api/machines/join", data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            answer = json.load(r)
    except urllib.error.HTTPError as e:
        store.revoke(hub)
        try:
            why = json.load(e).get("error")
        except ValueError:
            why = None
        raise SystemExit(f"lariat: the hub said no: {why or e.code}")
    except (urllib.error.URLError, OSError) as e:
        store.revoke(hub)
        raise SystemExit(f"lariat: cannot reach {base} ({getattr(e, 'reason', e)})")
    print(f"Added {answer['name']} to {hub}: it shows on your phone in a few seconds.")
    print(f"Undo: remove it on the phone (tap the machine name, hold {answer['name']}), or `lariat link --revoke {hub}` here.")


def publicly_trusted(url: str) -> bool:
    """Does our HTTPS verify without extra trust? (False if we cannot tell.)"""
    import urllib.error
    import urllib.request

    from . import tls

    try:
        urllib.request.urlopen(url + "/healthz", timeout=5, context=tls.public_context()).close()
        return True
    except urllib.error.HTTPError:
        return True  # the TLS handshake went through
    except (urllib.error.URLError, OSError):
        return False


def cmd_machines(args) -> None:
    from . import links, machines

    state = state_dir(args.state)
    registry = machines.Registry(state / "machines.json")
    if args.action == "list":
        entries = registry.load()
        if not entries:
            print("No other machines. On one, run `lariat link`, then `lariat machines add <code>` here.")
        for e in entries:
            print(f"{e['name']}  {e['url']}{'  (own CA)' if e.get('ca') else ''}")
        return
    if args.action == "remove":
        entry = next((e for e in registry.load() if e["name"] == args.target), None)
        if entry is None:
            raise SystemExit(f"lariat: no machine named {args.target}")
        try:  # have it revoke our link too
            req = urllib.request.Request(entry["url"] + "/api/unlink", data=b"{}", headers={"Authorization": f"Bearer {entry['token']}"}, method="POST")
            urllib.request.urlopen(req, timeout=5, context=machines.ssl_context(entry.get("ca"))).close()
            revoked = " (and its link to this hub)"
        except (urllib.error.URLError, OSError):
            revoked = f"; it was not reachable, so revoke the link there: `lariat link --revoke ...`"
        registry.remove(args.target)
        print(f"Removed {args.target}{revoked}.")
        return
    try:
        data = links.decode_code(args.target)
        name = machines.check_name(args.name or data.get("name") or "")
        app_name = machines.probe(data["url"], data["token"], data.get("ca"))
    except ValueError as e:
        raise SystemExit(f"lariat: {e}")
    registry.add(name, data["url"], data["token"], data.get("ca"))
    print(f"Added {name} ({app_name} at {data['url']}). It shows on the phone's dashboard in a few seconds.")


def cmd_update(args) -> None:
    from . import __version__, updates

    state = state_dir(args.state)
    how = updates.kind()
    settings = updates.Settings(state / "update.json", updates.default_mode(how))
    if args.mode:
        if how == "nix":
            raise SystemExit("lariat: this lariat comes from Nix; it updates with your flake input")
        settings.set_mode(args.mode)
        print({"auto": "Updates install on their own (when no terminal is open).", "notify": "lariat checks for updates and shows them; you install them.", "off": "lariat never checks for updates."}[args.mode])
        return
    if how == "nix":
        print(f"lariat {__version__}, from Nix: update your flake input (`nix flake update lariat`) and rebuild.")
        return
    try:
        latest = updates.latest_version()
    except (updates.UpdateError, OSError) as e:
        raise SystemExit(f"lariat: could not check for updates: {e}")
    newer = updates.is_newer(latest, __version__)
    print(f"lariat {__version__}; latest release {latest}; updates: {settings.mode()}.")
    if args.check or not newer:
        if newer and how != "binary":
            print(f"Update with: pip install -U {updates.BASE}/releases/download/v{latest}/lariat-{latest}-py3-none-any.whl")
        elif not newer:
            print("Up to date.")
        return
    if how != "binary":
        raise SystemExit(f"lariat: installed with pip; update with: pip install -U {updates.BASE}/releases/download/v{latest}/lariat-{latest}-py3-none-any.whl")
    # The running service updates itself and restarts; without one, do it here.
    try:
        token = (state / "notify-token").read_text().strip()
        local_request(state, args.port, "/api/update", {"op": "apply"}, {"X-Lariat-Notify": token})
        print(f"The running lariat is updating to {latest}; it restarts on its own (the phone reconnects).")
        return
    except urllib.error.HTTPError as e:
        raise SystemExit(f"lariat: the running gateway refused ({e.code})")
    except OSError:
        pass  # not running
    target = Path(sys.executable)
    try:
        updates.install(updates.download(latest, target.parent), target)
    except (updates.UpdateError, OSError) as e:
        raise SystemExit(f"lariat: update failed: {e}")
    print(f"Updated {target} to lariat {latest}.")


def operator_missing(tailscale: str) -> bool:
    """Linux: may this user set up `tailscale serve`? (The operator, or root.)"""
    if not sys.platform.startswith("linux") or os.geteuid() == 0:
        return False
    try:
        prefs = json.loads(subprocess.run([tailscale, "debug", "prefs"], capture_output=True, text=True, timeout=10).stdout)
    except (subprocess.SubprocessError, ValueError, OSError):
        return False  # cannot tell: `lariat doctor` will say if it fails
    import getpass

    return prefs.get("OperatorUser") != getpass.getuser()


def cmd_setup(args) -> None:
    """One guided pass for a new machine: Tailscale, HTTPS, the background
    service, the phone, agent alerts. --yes takes the defaults."""
    import time

    from . import doctor, install, tls

    state = state_dir(args.state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    interactive = not args.yes and sys.stdin.isatty()

    def ask(question: str, default: bool) -> bool:
        if not interactive:
            return default
        answer = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not answer else answer.startswith("y")

    def step(text: str) -> None:
        print(f"\n== {text}")

    print("lariat setup: your coding agents on your phone, over your tailnet.")
    me = "lariat" if shutil.which("lariat") else " ".join(install.self_command())

    step("Tailscale")
    info = tailnet()  # stops with what to do if it is missing, logged out or without MagicDNS
    print(f"ok: this machine is {info['name']}")
    if operator_missing(info["tailscale"]):
        print("lariat puts itself behind `tailscale serve`, which needs your permission once. Run:\n    sudo tailscale set --operator=$USER")
        if interactive:
            input("Press Enter when done. ")
            if operator_missing(info["tailscale"]):
                raise SystemExit("lariat: still not allowed; run the command above, then `lariat setup` again")
        else:
            raise SystemExit("lariat: run the command above, then `lariat setup` again")

    joining = bool(args.join) or (interactive and ask("Does your phone already use lariat on another machine?", default=False))

    if not joining:
        step("HTTPS (passkeys need it)")
        if info["https"]:
            print("ok: Tailscale's HTTPS certificates are on")
        elif tls.paths(state)["cert"].exists():
            print("ok: lariat's own CA (Tailscale's HTTPS certificates are off)")
        else:
            print(
                "Tailscale's HTTPS certificates are off on your tailnet. Either:\n"
                "  1. turn them on: https://login.tailscale.com/admin/dns -> HTTPS Certificates -> Enable\n"
                "     (one switch; your machine's name then appears in public Certificate Transparency logs), or\n"
                "  2. use lariat's own CA: nothing public, and your phone installs one certificate."
            )
            if interactive and input("Turned them on? Press Enter to check, or type 2 for the own CA: ").strip() != "2":
                for _ in range(3):
                    info = tailnet()
                    if info["https"]:
                        break
                    input("Still off (it can take a minute). Press Enter to check again. ")
            print("ok: Tailscale's certificates" if info["https"] else "ok: lariat makes its own CA when it starts")

    step("Background service")
    if args.no_service:
        print(f"skipped: run `{me} serve` yourself")
    else:
        install.install_service([], quiet=True)
        print("ok: installed; it starts with your login" + (" (keep it running when logged out: `loginctl enable-linger $USER`)" if sys.platform.startswith("linux") else ""))
        for _ in range(60):
            if gateway_info(state, args.port):
                break
            time.sleep(1)
        else:
            print("lariat does not answer yet; what `lariat doctor` sees:")
            doctor.report([c for c in doctor.run(state, args.port, args.https_port) if c["status"] in ("fail", "warn")], False)
            raise SystemExit(1)
        print("ok: lariat is running")

    if joining:
        step("Add it to your hub")
        print("On the phone (the lariat app): tap the machine name at the top left -> Add a machine.")
        line = " ".join(args.join or []) or (input("Paste the command it shows (or its code): ").strip() if interactive else "")
        words = [w for w in line.split() if w not in ("lariat", "join") and not w.endswith(("lariat", "lariat.exe"))]
        if len(words) < 2:
            raise SystemExit(f"lariat: that needs the hub's name and the code; later: `{me} join <hub> <code>`")
        cmd_join(argparse.Namespace(hub=words[0], code="".join(words[1:]), name=args.name, ca=None, host=None,
                                    port=args.port, https_port=args.https_port, state=args.state))
    else:
        step("Your phone")
        before = enrolled(state)
        if own_ca_in_use(state, info["https"]):
            ip = next((a for a in info["ips"] if ":" not in a), None)
            if ip and tls.paths(state)["ca"].exists():
                import threading

                server = phone_server(state, info["name"], ip, 8411, args.https_port)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                show_qr(f"http://{info['name']}:8411/", "First, let the phone trust this machine: scan this, install the profile, then\n"
                        "Settings -> General -> About -> Certificate Trust Settings -> turn it on.")
                if interactive:
                    input("Press Enter when it is trusted. ")
                server.shutdown()
        if not before or ask(f"{before} device(s) enrolled already. Enroll another?", default=False):
            show_qr(enroll_url(info["name"], fresh_token(state), args.https_port),
                    "Scan it with the phone's camera, tap Create passkey, then Share -> Add to Home Screen,\n"
                    "open the app from the Home Screen and tap Unlock.")
            if interactive:
                print("Waiting for your phone (Ctrl-C to go on without it)...")
                try:
                    while enrolled(state) <= before:
                        time.sleep(1)
                    print("ok: phone enrolled")
                except KeyboardInterrupt:
                    print()
        print("Then, in the app: ⋮ -> Push notifications -> Allow -> Send a test.")

    step("Agent alerts")
    found = False
    if shutil.which("claude") or install.claude_settings().parent.exists():
        found = True
        if install.claude_hooks_installed():
            print("ok: Claude Code sessions alert your phone")
        elif ask("Alert your phone when Claude Code sessions finish or need you?", default=True):
            install.install_claude_hooks(uploads=state / "uploads")
            print("ok: new Claude Code sessions report to lariat")
    if shutil.which("grok"):
        found = True
        if ask("Alert your phone when Grok Build sessions finish?", default=True):
            install.install_grok_hooks()
            print("ok: Grok Build sessions report to lariat")
    if not found:
        print("No Claude Code or Grok here yet; later: `lariat hooks claude --install`, or from the phone (hold the machine name).")

    problems = [c for c in doctor.run(state, args.port, args.https_port) if c["status"] in ("fail", "warn")]
    step("Done" if not problems else "Done, but check these")
    if problems:
        doctor.report(problems, False)
    print("More machines: run the installer and `lariat setup` on each, and answer yes to the first question.")


def cmd_serve(args) -> None:
    from . import tls

    state = state_dir(args.state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    own_tls = tls.paths(state)
    use_tls = own_tls["cert"].exists() and not args.tailscale_cert
    if args.host:
        host, info = args.host, None
    else:
        info = tailnet()
        host = info["name"]
        if use_tls and info["https"] and tls.made_automatically(state):
            # That CA was only for a tailnet without certificates; it has them now.
            use_tls = False
            print("lariat: Tailscale's HTTPS certificates are on now; using them (not the CA lariat made earlier).", file=sys.stderr)
        if not info["https"] and not use_tls and not args.tailscale_cert:
            # No Tailscale certificates on this tailnet: our own CA, for this name only.
            tls.init(state, host, automatic=True)
            own_tls, use_tls = tls.paths(state), True
            print(
                "lariat: Tailscale's HTTPS certificates are off for this tailnet, so lariat made its own CA\n"
                "for this machine. Linked to a hub, nothing more is needed; if your phone uses this machine\n"
                "itself, install the CA on it: `lariat tls phone`.",
                file=sys.stderr,
            )
    if info and not args.no_tailscale_serve:
        if use_tls:  # we terminate TLS; Tailscale forwards bytes
            tailscale_unserve(info["tailscale"], f"--https={args.https_port}")
            tailscale_serve(info["tailscale"], [f"--tcp={args.https_port}", f"tcp://127.0.0.1:{args.port}"])
        else:
            tailscale_unserve(info["tailscale"], f"--tcp={args.https_port}")
            tailscale_serve(info["tailscale"], [f"--https={args.https_port}", f"http://127.0.0.1:{args.port}"])

    env = os.environ
    env.setdefault("LARIAT_HOST", host)
    if args.https_port != 443:
        env.setdefault("LARIAT_ORIGIN", f"https://{host}:{args.https_port}")
    env["LARIAT_PORT"] = str(args.port)
    env["LARIAT_STATE_DIR"] = str(state)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    env.setdefault("LARIAT_RUNTIME_DIR", str(Path(runtime) / "lariat" if runtime else state / "run"))
    env.setdefault("LARIAT_ENROLL", "1")  # one-time codes from `lariat enroll`
    env.setdefault("LARIAT_APP_NAME", args.name)
    if use_tls:
        env.setdefault("LARIAT_TLS_CERT", str(own_tls["cert"]))
        env.setdefault("LARIAT_TLS_KEY", str(own_tls["key"]))
        env.setdefault("LARIAT_CERT_FILE", str(own_tls["cert"]))
    for var, tool in (("LARIAT_TMUX", "tmux"), ("LARIAT_HERDR", "herdr")):
        found = shutil.which(tool)
        if found and var not in env:
            env[var] = found
    whisper = env.get("LARIAT_DICTATE_SERVER") or shutil.which("whisper-server")
    if env.get("LARIAT_DICTATE_MODEL") and whisper and "LARIAT_DICTATE" not in env:
        env["LARIAT_DICTATE"] = "1"
        env["LARIAT_DICTATE_SERVER"] = whisper

    port = "" if args.https_port == 443 else f":{args.https_port}"
    how = "your own CA" if use_tls else "the ts.net certificate"
    print(f"lariat: https://{host}{port}/ ({how})", file=sys.stderr)
    if not enrolled(state) and "LARIAT_CREDENTIALS_FILE" not in env:
        print("No passkey yet. Enroll your phone:", file=sys.stderr)
        sys.stdout.flush()
        show_qr(enroll_url(host, fresh_token(state), args.https_port), ENROLL_HINT)
    else:
        print("Add another device with `lariat enroll`.", file=sys.stderr)

    from .__main__ import main as run_gateway

    run_gateway()


def main(argv: list[str] | None = None) -> None:
    from . import __version__

    parser = argparse.ArgumentParser(prog="lariat", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"lariat {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    def common(p, host_help="DNS name the phone uses (default: this node's MagicDNS name)", port_help="loopback port of the gateway"):
        p.add_argument("--host", help=host_help)
        p.add_argument("--port", type=int, default=int(os.environ.get("LARIAT_PORT", "8795")), help=port_help)
        p.add_argument("--https-port", type=int, default=int(os.environ.get("LARIAT_HTTPS_PORT", "443")), help="tailnet HTTPS port")
        p.add_argument("--state", help="state directory")

    serve = sub.add_parser("serve", help="run lariat (the default)")
    common(serve)
    serve.add_argument("--no-tailscale-serve", action="store_true", help="don't configure tailscale serve (you front it yourself)")
    serve.add_argument("--tailscale-cert", action="store_true", help="use the ts.net certificate even if `lariat tls init` was run")
    serve.add_argument("--name", default="lariat", help="app name on the phone's home screen")
    enroll = sub.add_parser("enroll", help="show a QR code to enroll a device")
    common(enroll)
    enroll.add_argument("--new", action="store_true", help="make a new one-time code even if the last is unused")
    notify = sub.add_parser("notify", help="send an alert to your phone (inbox and push)")
    notify.add_argument("message", nargs="*", help="the alert text")
    notify.add_argument("--kind", choices=["done", "blocked", "info"], default="info")
    notify.add_argument("--agent", help="who it is from, e.g. claude")
    notify.add_argument("--project", help="what it is about, e.g. the repository")
    notify.add_argument("--title", help="the notification title (default: from agent and kind)")
    notify.add_argument("--hook", nargs=2, metavar=("AGENT", "EVENT"), help="read a Claude Code / Grok hook's JSON on stdin; EVENT is stop or notification")
    notify.add_argument("--port", type=int, default=int(os.environ.get("LARIAT_PORT", "8795")), help="loopback port of the gateway")
    notify.add_argument("--state", help="state directory")
    doctor_cmd = sub.add_parser("doctor", help="check everything and say what to fix")
    common(doctor_cmd)
    doctor_cmd.add_argument("--json", action="store_true", help="machine-readable, for agents")
    hooks = sub.add_parser("hooks", help="report agent sessions to lariat (inbox and push)")
    hooks.add_argument("agent", choices=["claude", "grok"])
    hooks.add_argument("--install", action="store_true", help="write the hooks (Claude: merged into settings.json, with a backup)")
    hooks.add_argument("--settings", help="Claude Code settings file (default: ~/.claude/settings.json)")
    hooks.add_argument("--command", help="the lariat the hooks run (default: this one), e.g. a stable profile path")
    service = sub.add_parser("service", help="run lariat in the background (systemd, launchd, Windows Startup)")
    service.add_argument("action", choices=["install", "uninstall"])
    service.add_argument("--dry-run", action="store_true", help="print what would be written and run")
    service.add_argument("serve_args", nargs=argparse.REMAINDER, help="after --: options for `lariat serve`")
    join = sub.add_parser("join", help="add this machine to your hub, with the code from the phone (Add a machine)")
    join.add_argument("hub", help="the hub's name, e.g. desktop (as the phone shows it)")
    join.add_argument("code", help="the code from the phone")
    common(join, host_help="this machine's DNS name, as the hub reaches it (default: its MagicDNS name)")
    join.add_argument("--name", help="show this machine under another name (default: its host name)")
    join.add_argument("--ca", help="the CA certificate this machine's HTTPS uses, if not `lariat tls init`'s")
    link = sub.add_parser("link", help="let your hub (the machine your phone uses) reach this machine")
    common(link)
    link.add_argument("--name", default="hub", help="what to call the hub's access here (default: hub)")
    link.add_argument("--ca", help="the CA certificate this machine's HTTPS uses, if not `lariat tls init`'s (e.g. the NixOS nginx frontend)")
    link.add_argument("--list", action="store_true", help="list the hubs that may reach this machine")
    link.add_argument("--revoke", metavar="NAME", help="cut a hub off")
    machines_cmd = sub.add_parser("machines", help="on the hub: the other machines the phone reaches through it")
    machines_cmd.add_argument("action", choices=["add", "list", "remove"])
    machines_cmd.add_argument("target", nargs="?", help="add: the link code from `lariat link`; remove: the machine's name")
    machines_cmd.add_argument("--name", help="add: show it under this name (default: its host name)")
    machines_cmd.add_argument("--state", help="state directory")
    setup = sub.add_parser("setup", help="start here: set up this machine and your phone, step by step")
    common(setup)
    setup.add_argument("--yes", action="store_true", help="take the defaults, ask nothing")
    setup.add_argument("--no-service", action="store_true", help="do not install the background service")
    setup.add_argument("--join", nargs=2, metavar=("HUB", "CODE"), help="add this machine to your hub (the phone's Add a machine)")
    setup.add_argument("--name", help="with --join: show this machine under another name")
    update = sub.add_parser("update", help="update to the latest release (release binaries)")
    update.add_argument("--check", action="store_true", help="only say whether there is a newer release")
    update.add_argument("--mode", choices=["auto", "notify", "off"], help="auto: install updates on their own; notify: only show them; off: never check")
    update.add_argument("--port", type=int, default=int(os.environ.get("LARIAT_PORT", "8795")), help="loopback port of the gateway")
    update.add_argument("--state", help="state directory")
    tls_cmd = sub.add_parser("tls", help="your own CA instead of the ts.net certificate")
    tls_sub = tls_cmd.add_subparsers(dest="tls_cmd", required=True)
    init = tls_sub.add_parser("init", help="make the CA and a certificate for this node")
    common(init)
    init.add_argument("--renew", action="store_true", help="issue a new server certificate (same CA)")
    phone = tls_sub.add_parser("phone", help="serve the CA certificate for the phone (Ctrl-C to stop)")
    common(phone, port_help="port of the page for the phone, on the tailnet address (default: 8411)")
    phone.set_defaults(port=8411)

    if sys.platform == "win32":  # QR codes and the odd arrow; Windows consoles default to a legacy codepage
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("serve", "enroll", "tls", "notify", "doctor", "hooks", "service", "link", "machines", "update", "join", "setup", "-h", "--help", "--version"):
        argv.insert(0, "serve")  # `lariat` alone, or with serve's flags
    args = parser.parse_args(argv)
    if args.cmd == "tls":
        (cmd_tls_init if args.tls_cmd == "init" else cmd_tls_phone)(args)
    elif args.cmd == "machines" and args.action != "list" and not args.target:
        parser.error(f"machines {args.action} needs {'a link code' if args.action == 'add' else 'a name'}")
    elif args.cmd in ("notify", "doctor", "hooks", "service", "link", "machines", "update", "join", "setup"):
        {
            "notify": cmd_notify,
            "doctor": cmd_doctor,
            "hooks": cmd_hooks,
            "service": cmd_service,
            "link": cmd_link,
            "machines": cmd_machines,
            "update": cmd_update,
            "join": cmd_join,
            "setup": cmd_setup,
        }[args.cmd](args)
    else:
        (cmd_enroll if args.cmd == "enroll" else cmd_serve)(args)


if __name__ == "__main__":
    main()
