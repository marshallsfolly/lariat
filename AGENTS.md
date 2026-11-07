# AGENTS.md

For coding agents. Two jobs: **setting lariat up for your user** (most
likely, if they pointed you here) and **working on this repository**.

## Setting lariat up for your user

The goal: your user opens lariat on their phone over Tailscale, unlocked with
a passkey, sees their agents, gets push notifications, and (optionally)
dictates. Human docs: [docs/setup.md](docs/setup.md); details on HTTPS:
[docs/tls.md](docs/tls.md); NixOS: [docs/nixos.md](docs/nixos.md).

### Ground rules

- **lariat is a shell on this machine for whoever unlocks it.** Keep it on the
  tailnet: never `tailscale funnel`, never a public reverse proxy, never bind
  it to `0.0.0.0` or a LAN address.
- Run it as the user, never as root. The only command that needs `sudo` is
  `sudo tailscale set --operator=$USER` (Linux, once): ask before running it.
- **Ask first** before: picking the HTTPS mode (below), installing a background
  service, adding hooks to the user's Claude Code settings, editing a NixOS or
  home-manager configuration, or deploying it.
- The enrollment link/QR code is a one-time credential. Show it to your user
  and nowhere else (no issues, commits, logs or other chats).
- Never delete lariat's state directory (`~/.local/state/lariat`,
  `%LOCALAPPDATA%\lariat` on Windows): it holds the passkeys and, with the own
  CA, the CA.
- Some steps need the user's hands: Tailscale login and admin-console
  switches, scanning QR codes, installing a CA profile, Face ID, "Add to Home
  Screen". Say exactly what to tap, then wait for them to confirm.

### Steps

`lariat doctor --json` drives the whole thing: each check has an `id`, a
`status` (`ok`, `warn`, `fail`, `info`) and, unless it is ok, a `fix`. Run it
after every step and work through what is left.

1. **Get lariat.** Is it on PATH (`lariat --version`)? If not, ask how they
   want it:
   - a release binary (default), with the installer: it picks the binary,
     checks it against the release's `SHA256SUMS` and puts it on PATH.
     `LARIAT_NO_SETUP=1` skips its interactive `lariat setup`, which is
     for people, not agents:

     ```sh
     curl -fsSL https://raw.githubusercontent.com/marshallsfolly/lariat/main/install.sh | LARIAT_NO_SETUP=1 sh
     ```

     Windows (PowerShell):

     ```powershell
     $env:LARIAT_NO_SETUP = '1'; irm https://raw.githubusercontent.com/marshallsfolly/lariat/main/install.ps1 | iex
     ```

     (By hand: docs/setup.md step 2.)
   - with Nix: `nix run github:marshallsfolly/lariat -- <command>`, or the
     home-manager / NixOS module (docs/nixos.md). With a module, edit their
     configuration and let them deploy.
2. **Tailscale.** Installed, connected, MagicDNS on (`tailscale.*` checks). The
   install and login are theirs; MagicDNS is a switch at
   https://login.tailscale.com/admin/dns. On Linux, `tailscale serve` needs
   `sudo tailscale set --operator=$USER` once: ask them to run it (or ask
   before you do) before step 4, or the service starts and fails quietly.
3. **HTTPS.** Ask: "Tailscale's certificate (one switch, but your machine's
   name becomes public in Certificate Transparency logs) or your own CA
   (nothing public, you install one certificate on the phone)?"
   - Tailscale's: they turn on HTTPS certificates on the DNS page, before
     step 4.
   - Own CA: nothing to run; with the certificates off, lariat makes its own
     CA when it starts (step 4). After that, run `lariat tls phone` in the
     background (it serves until stopped), show its QR code or link, and walk
     them through docs/tls.md "Installing the CA on the phone". Stop it when
     they confirm.
4. **Run it.** If they want it always on (usually yes), `lariat service install`
   (pass serve options after `--`). Otherwise `lariat serve` in a terminal they
   keep. Then `lariat doctor` must show lariat running; if not, its log says
   why (docs/troubleshooting.md).
5. **Enroll the phone.** Run `lariat enroll`: it prints a QR code and the
   link. They scan it with the camera, tap Create passkey, then Share → Add to
   Home Screen, open the app and tap Unlock.
6. **Notifications.** In the installed app: ⋮ → Push notifications → Allow →
   Send a test. If they use Claude Code, offer `lariat hooks claude --install`
   (merges two hooks into `~/.claude/settings.json`, plus a rule that lets
   agents read the images the user sends from the phone; keeps a backup); new
   sessions then notify. Grok Build: `lariat hooks grok --install`.
7. **Dictation (optional).** Needs whisper.cpp's `whisper-server` (macOS:
   `brew install whisper-cpp`; Windows: a whisper.cpp release; Linux: their
   package manager, or the Nix `lariat-dictation` package) and a model, e.g.
   `ggml-base.en-q8_0.bin` (CPU) or `ggml-small.en-q8_0.bin` (GPU) from
   https://huggingface.co/ggerganov/whisper.cpp. Set `LARIAT_DICTATE_SERVER`
   and `LARIAT_DICTATE_MODEL`, then re-run `lariat service install` from that
   shell so the service keeps them.
8. **Check.** `lariat doctor` until nothing fails; ask them to confirm the
   dashboard opens on the phone and a test notification arrives.
9. **Updates.** Release binaries update themselves (installing when no
   terminal is open); tell the user, and offer `lariat update --mode notify`
   if they would rather approve each one. Nix and pip installs update the way
   they were installed.
10. **More machines (optional).** The phone keeps one app; one machine (the
   hub, usually the first) reaches the others. On each other machine, steps
   1, 2 and 4 (no HTTPS choice, no enrolling, no dictation). Then the user taps the machine name on the phone
   → **Add a machine**, and you run the `lariat join <hub> <code>` it shows
   on the new machine (the code is a one-time credential: use it, never log or
   share it). Then they hold the machine there to turn on its agents' alerts.
   Details: [docs/machines.md](docs/machines.md).

## Working on this repository

- `gateway/`: Python 3.11+, aiohttp. Auth (WebAuthn), sessions, providers
  (shell, tmux, herdr, alerts), the PTY bridge (POSIX PTY or Windows ConPTY),
  links and machines (`links.py`, `machines.py`: one hub reaching the others),
  dictation (whisper-server), Web Push, the `lariat` command
  (`cli.py`, `doctor.py`, `install.py`, `tls.py`).
- `web/`: the PWA, vanilla ES modules and one stylesheet, bundled by
  `web/build.py`. No CDN, no frameworks.
- `nix/`: packages, the NixOS and home-manager modules, the theme.
- Tests: `cd gateway && python -m unittest discover -s tests -t . -v`
  (dependencies: `pip install ./gateway`, or `nix develop`). They must pass on
  Linux, macOS and Windows; skip a test on a platform only when the feature
  cannot exist there.
- Builds: `nix build .#gateway .#web .#lariat` (the gateway's tests run in its
  build). CI runs both on every push; tags `v*` publish release binaries.
- Keep: everything on the tailnet; strict CSP; inputs validated, herdr calls
  from a fixed allowlist, client JSON never forwarded; Web Push only to known
  push services; no audio or text in logs.
- Never commit media (videos, recordings) or anything identifying a person or
  machine (host names, tailnet names, IPs, user names, emails); examples use
  `example.ts.net` and `100.64.0.0/10` addresses.
- Docs live in `README.md` and `docs/`; update them with behaviour changes.
