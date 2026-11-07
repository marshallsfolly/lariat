"""`lariat doctor`: is everything in place to use lariat from the phone?

One line per check, with the fix when something is missing. `--json` is for
coding agents setting lariat up for someone: every check has an id, a status
(ok, warn, fail, info), a message, and a fix to run or tell the user.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
NIXOS_UNIT = Path("/etc/systemd/user/lariat.service")  # the NixOS module runs lariat
SYMBOL = {OK: "✓", WARN: "!", FAIL: "✗", INFO: "·"}


def check(cid: str, status: str, message: str, fix: str | None = None) -> dict:
    item = {"id": cid, "status": status, "message": message}
    if fix:
        item["fix"] = fix
    return item


def _json(cmd: list[str]) -> dict:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        return json.loads(out) if out.strip() else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def run(state: Path, port: int, https_port: int = 443) -> list[dict]:
    from . import cli, install, tls

    module = NIXOS_UNIT.exists()
    where = "the NixOS module" if module else f"state in {state}"
    out = [check("lariat", INFO, f"lariat {__version__} on {sys.platform}, {where}")]

    tailscale = cli.find_tailscale()
    status: dict | None = None
    if not tailscale:
        out.append(check("tailscale.installed", FAIL, "Tailscale is not installed", "Install it from https://tailscale.com/download and log in"))
    else:
        status = _json([tailscale, "status", "--json"])
        backend = status.get("BackendState")
        if backend == "Running":
            out.append(check("tailscale.running", OK, "Tailscale is connected"))
        else:
            out.append(check("tailscale.running", FAIL, f"Tailscale is not connected ({backend or 'no answer'})", "Run `tailscale up` (or open the Tailscale app) and log in"))
        name = ((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
        if name:
            out.append(check("tailscale.magicdns", OK, f"This machine's name: {name}"))
        else:
            out.append(check("tailscale.magicdns", FAIL, "No MagicDNS name", "Turn on MagicDNS at https://login.tailscale.com/admin/dns"))

    own = tls.paths(state)
    if module:
        out.append(check("https", INFO, "HTTPS: set up by the NixOS module (services.lariat.frontend)"))
    elif cli.own_ca_in_use(state, bool(status and status.get("CertDomains"))):
        days = tls.days_left(own["cert"])
        made = " (made because the tailnet's HTTPS certificates are off)" if tls.made_automatically(state) else ""
        out.append(
            check("https", OK if days > 30 else WARN, f"HTTPS with your own CA{made}; the certificate is good for {days} more days",
                  "A phone that uses this machine directly must trust the CA: `lariat tls phone`" if days > 30
                  else "Renew: `lariat tls init --renew`, then restart lariat")
        )
    elif status and status.get("CertDomains"):
        out.append(check("https", OK, "HTTPS with Tailscale's certificate (HTTPS certificates are on)"))
    else:
        out.append(
            check("https", WARN, "No HTTPS yet; the phone needs it for passkeys",
                  "Turn on HTTPS certificates at https://login.tailscale.com/admin/dns (one switch), "
                  "or leave them off: lariat then makes its own CA when it starts (`lariat tls phone` puts it on the phone)")
        )

    if tailscale and status and status.get("BackendState") == "Running" and not module:
        serve = _json([tailscale, "serve", "status", "--json"])
        if f"127.0.0.1:{port}" in json.dumps(serve):
            out.append(check("tailscale.serve", OK, f"tailscale serve forwards port {https_port} to lariat"))
        else:
            fix = "Start lariat (`lariat serve`); it sets this up"
            if sys.platform.startswith("linux"):
                fix += ". If it reports a permission problem: `sudo tailscale set --operator=$USER` (once)"
            out.append(check("tailscale.serve", WARN, "tailscale serve does not point at lariat yet", fix))

    info = cli.gateway_info(state, port)
    if info is None and (module or install.service_installed()):
        log = "~/Library/Logs/lariat.log" if sys.platform == "darwin" else "journalctl --user -u lariat" if sys.platform != "win32" else "its window"
        fix = f"The service is installed but lariat is not answering: see {log}"
        if sys.platform.startswith("linux"):
            fix += ". Most often: `sudo tailscale set --operator=$USER` (once), then `systemctl --user restart lariat`"
        out.append(check("gateway", FAIL, f"lariat is not running on port {port}", fix))
    elif info is None:
        out.append(check("gateway", WARN, f"lariat is not running on port {port}", "`lariat service install` keeps it running (or `lariat serve` in a terminal)"))
    else:
        out.append(check("gateway", OK, f"lariat is running (enrollment {'open' if info.get('enroll') else 'closed'})"))

    n = cli.enrolled(state)
    out.append(
        check("passkeys", OK if n else WARN, f"{n} device(s) enrolled" if n else "No phone enrolled yet",
              None if n else "Run `lariat enroll` and scan the QR code with the phone's camera")
    )
    try:
        subs = len(json.loads((state / "push.json").read_text()))
    except (OSError, ValueError, TypeError):
        subs = 0
    out.append(
        check("push", OK if subs else INFO, f"{subs} device(s) get push notifications" if subs else "No device has push notifications on",
              None if subs else "In the installed app: ⋮ → Push notifications (iPhone: iOS 16.4+, app on the Home Screen)")
    )

    for tool, what in (("tmux", "tmux dashboard"), ("herdr", "agent dashboard (herdr)")):
        found = shutil.which(tool)
        out.append(check(f"tools.{tool}", INFO, f"{what}: {'uses ' + found if found else 'not installed'}"))
    whisper = os.environ.get("LARIAT_DICTATE_SERVER") or shutil.which("whisper-server")
    model = os.environ.get("LARIAT_DICTATE_MODEL")
    ready = bool(whisper and model)
    out.append(
        check("dictation", OK if ready else INFO, "Dictation: ready" if ready else "Dictation: off",
              None if ready else "Set LARIAT_DICTATE_SERVER (whisper.cpp's whisper-server) and LARIAT_DICTATE_MODEL (a ggml model); docs/setup.md")
    )
    hooked = install.claude_hooks_installed()
    out.append(
        check("hooks.claude", OK if hooked else INFO,
              "Claude Code reports to lariat" if hooked else "Claude Code sessions outside herdr do not notify the phone",
              None if hooked else "`lariat hooks claude --install`")
    )
    svc = module or install.service_installed()
    out.append(
        check("service", OK if svc else INFO, "Runs as a background service" if svc else "Not set up as a background service",
              None if svc else "`lariat service install`")
    )
    return out


def report(checks: list[dict], as_json: bool) -> int:
    failed = any(c["status"] == FAIL for c in checks)
    if as_json:
        print(json.dumps({"ok": not failed, "checks": checks}, indent=2))
    else:
        for c in checks:
            print(f"{SYMBOL[c['status']]} {c['message']}")
            if c.get("fix") and c["status"] != OK:
                print(f"    → {c['fix']}")
    return 1 if failed else 0
