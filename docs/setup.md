# Getting started

From nothing to your phone talking to your agents, on Linux, macOS or
Windows. NixOS has its own guide: [nixos.md](nixos.md). Would rather have
your coding agent do it? See [Let your agent set it up](../README.md#let-your-agent-set-it-up).

**The quick way** does all of the steps below with you, asking only what it
must:

```sh
curl -fsSL https://raw.githubusercontent.com/marshallsfolly/lariat/main/install.sh | sh     # Linux, macOS
```

```powershell
irm https://raw.githubusercontent.com/marshallsfolly/lariat/main/install.ps1 | iex          # Windows (PowerShell)
```

It installs the binary and runs `lariat setup`. The rest of this page is the
long form: each step by hand, and what it does. At any point,
`lariat doctor` checks everything and says what to fix next.

## 1. Tailscale on the machine and the phone

lariat is reachable only on your tailnet, so both ends need Tailscale.

1. Install [Tailscale](https://tailscale.com/download) on the machine where
   your agents run, and log in.
2. Install the Tailscale app on your phone and log in to the **same** tailnet.
   Keep it connected when you use lariat.
3. **MagicDNS** must be on (it is, on new tailnets; the
   [DNS page](https://login.tailscale.com/admin/dns) has the switch). Your
   machine then has a name like `myhost.your-tailnet.ts.net`: the address the
   phone opens. Your passkey is tied to it, so if you want a different machine
   name, rename it now, before you enroll.
4. **Linux**: lariat puts itself behind `tailscale serve`, which needs your
   permission once: `sudo tailscale set --operator=$USER`.
5. If anyone else is on your tailnet, use an
   [ACL](https://tailscale.com/kb/1018/acls) so only your devices reach this
   machine. lariat is a shell on it.

## 2. Install lariat

The installer above does this. By hand, from
[Releases](https://github.com/marshallsfolly/lariat/releases):

```sh
# Linux or macOS: pick one of linux-x86_64, linux-arm64, macos-arm64, macos-x86_64
T=linux-x86_64
mkdir -p ~/.local/bin
curl -fL -o ~/.local/bin/lariat "https://github.com/marshallsfolly/lariat/releases/latest/download/lariat-$T"
chmod +x ~/.local/bin/lariat
```

`~/.local/bin` must be on your PATH (on macOS it usually is not:
`echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc`, then open a new
terminal). `SHA256SUMS` in the release has the checksums.

Windows (PowerShell):

```powershell
$dir = "$env:LOCALAPPDATA\Programs\lariat"
New-Item -ItemType Directory -Force $dir | Out-Null
Invoke-WebRequest https://github.com/marshallsfolly/lariat/releases/latest/download/lariat-windows-x86_64.exe -OutFile "$dir\lariat.exe"
[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path', 'User') + ";$dir", 'User')
```

Then open a new PowerShell. SmartScreen may ask once ("More info" → "Run
anyway").

Other ways: `nix run github:marshallsfolly/lariat` to try it (for an always-on
install use the [home-manager or NixOS module](nixos.md)), or
`pip install lariat-*.whl` from Releases (Python 3.11+).

What you get:

- **Linux and macOS**: terminals are your login shell; the tmux dashboard if
  tmux is installed; the agent dashboard if [herdr](https://herdr.dev) is.
- **Windows**: terminals are PowerShell (ConPTY). tmux and herdr do not run on
  Windows, so there is no agent dashboard; terminals, dictation and
  notifications work.

## 3. HTTPS

Passkeys need an HTTPS page the phone trusts. Either:

- **Turn on Tailscale's certificates** (one switch): **HTTPS Certificates** on
  the [DNS page](https://login.tailscale.com/admin/dns). Nothing to install on
  the phone. Your machine's name ends up in public Certificate Transparency
  logs (known, not reachable).
- **Or leave them off**: lariat then makes its own CA when it first starts,
  for this one machine's name. Your phone installs it once:
  `lariat tls phone` shows a QR code and a page with the steps
  ([details](tls.md#your-own-ca)). If you turn the certificates on later,
  lariat switches to them by itself.

(`lariat tls init` makes the own CA on purpose: then lariat keeps using it
even with Tailscale's certificates on.)

## 4. Run it

```sh
lariat service install
```

That is a systemd user service on Linux, a launchd agent on macOS and a
Startup-folder entry on Windows, running `lariat serve` with the PATH and any
`LARIAT_*` settings of the shell you run it from, and starting with your
login. Options for `serve` go after `--`, e.g.
`lariat service install -- --name devbox`.

- Linux: `loginctl enable-linger $USER` keeps it running while you are logged
  out. Logs: `journalctl --user -u lariat -f`.
- macOS logs: `~/Library/Logs/lariat.log`. A sleeping Mac is unreachable;
  your phone's machine wants to be one that stays on.
- Windows: if Tailscale says access is denied, run the command lariat prints
  once in an administrator PowerShell.
- `lariat service uninstall` removes it; `--dry-run` shows what either would
  do.

(`lariat serve` runs it in the foreground instead; use one or the other.)

## 5. Enroll your phone

```sh
lariat enroll
```

It prints a QR code. On the phone:

1. Scan it with the **camera** and open the link (Safari on iPhone).
2. Tap **Create passkey** and confirm with Face ID or your fingerprint.
3. Install the app: Share → **Add to Home Screen** (iPhone), or ⋮ → **Install
   app** (Android Chrome). Open it from the Home Screen and tap **Unlock**.

The code works once; `lariat enroll` makes a new one for another device.

## 6. Notifications on the lock screen

In the installed app: ⋮ → **Push notifications** → Allow → **Send a test**,
then lock the phone. Choose alerts for "needs you", "finishes", or both.
(iPhone: iOS 16.4 or later, the app on the Home Screen.)

No account and no notification app: lariat signs pushes with its own key and
sends them to your phone's push service (Apple's or Google's), encrypted end
to end. While the app is open in front, alerts show inside it instead.

## 7. Agent alerts

- **herdr agents**: automatically, when one starts waiting for you or finishes.
- **Claude Code**: `lariat hooks claude --install` adds two hooks to
  `~/.claude/settings.json` (merged in; a backup is kept). New sessions report
  to lariat, inside herdr or not. Grok Build: `lariat hooks grok --install`.
  Or from the phone: tap the machine name, hold the machine, and turn alerts on.
- **Anything else**: `lariat notify --kind done --agent build "Release is out"`.

## 8. Check it

```sh
lariat doctor
```

Every line is ✓, or says what to do next.

## Using it

- **Agents**: the dashboard shows each [herdr](https://herdr.dev) agent's
  state and last lines; tap one for its terminal, hold it for more (prompt,
  rename, close). **+** starts a new one in a project.
- **The key bar**: Esc, Tab, sticky Ctrl and Alt, arrows, paste, image, Enter
  and the mic. Pinch to zoom the terminal.
- **Select and copy**: hold the terminal (or ⋮ → Select text). Its text opens
  as plain text where the phone's own selection works; agents and tmux bring
  their recent history.
- **Images**: the image key (take a photo or pick several), or copy a
  screenshot and tap paste. lariat shrinks each to 2048 px on the phone
  (dropping GPS and other metadata), stores it on the machine and types its
  path; Claude Code takes the path as the image. Uploads are deleted after 7
  days (`LARIAT_UPLOAD_DAYS`).
- **Several machines**: tap the machine name → **Add a machine**
  ([how](machines.md)).

### Dictation (optional)

lariat runs whisper.cpp's `whisper-server` on your machine. Point it at the
server and a model, then re-run `lariat service install` from that shell so
the service keeps the settings:

```sh
export LARIAT_DICTATE_SERVER=/path/to/whisper-server
export LARIAT_DICTATE_MODEL=/path/to/ggml-small.en-q8_0.bin
```

- Models: [base.en q8](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en-q8_0.bin)
  (fine on a CPU) or [small.en q8](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en-q8_0.bin)
  (better, wants a GPU).
- macOS: `brew install whisper-cpp` (Metal on Apple Silicon).
- Windows: [whisper.cpp releases](https://github.com/ggml-org/whisper.cpp/releases)
  have CPU, CUDA and Vulkan builds.
- Linux with Nix: `nix run github:marshallsfolly/lariat#lariat-dictation` bundles a
  CPU build and the base.en model.

In a terminal, tap the mic, talk, pause: the text is typed and Enter pressed.
Say just a number ("zero", "two") to press that key instead, for menus such as
Claude Code's "1 to review · 0 to dismiss"; no Enter follows it. The pause
length and the Enter are per-device settings in the terminal's ⋮ menu. With a
GPU the text lands about 50 ms after you stop; on a fast CPU, about half a
second. iOS asks for the microphone once each time the app starts.

## Day to day

**Another device**: `lariat enroll`, scan, Create passkey, install the app.

**Remove a device**: open `~/.local/state/lariat/enrolled.json`
(`%LOCALAPPDATA%\lariat\enrolled.json` on Windows), delete that device's entry
(each has a `label` and a `created` date), then restart lariat. Its sessions
end, and it gets no more notifications.

**Updates**: a release binary updates itself. Every 6 hours it looks for a
new release of github.com/marshallsfolly/lariat; it downloads the binary for this
platform, checks it against the release's `SHA256SUMS`, and installs it as
soon as no terminal is open on that machine (a restart ends open terminals;
tmux and herdr sessions carry on). The service then starts the new version.
On the phone, ⋮ → **Updates** shows every machine's version, with
**Update now** and a choice per machine: **Auto** (the default), **Notify**
(only show new releases) or **Off** (never contact GitHub). From a terminal:
`lariat update` (now), `lariat update --check`, `lariat update --mode notify`.
Passkeys and settings are kept; if the machine your phone uses restarts, unlock
the app again. With pip: `pip install -U` the release's wheel. With Nix: update the flake
input (`nix flake update lariat`) and rebuild; Nix installs never check.
The phone app itself offers "New version ready" after its machine updates.

**Back up**: the state directory holds your passkeys and, with your own CA, the
CA. Copy it if you want to restore without enrolling again.

**Uninstall**: `lariat service uninstall`; `tailscale serve --https=443 off`
(with your own CA: `tailscale serve --tcp=443 off`); delete the state
directory; delete the app from the phone, and with your own CA remove its
profile ([how](tls.md#removing-the-ca-from-the-phone)).

## Where things live

| | Linux, macOS | Windows |
|---|---|---|
| State: passkeys, keys, push subscriptions, your CA, images from the phone (`uploads/`) | `~/.local/state/lariat` | `%LOCALAPPDATA%\lariat` |
| Service | `~/.config/systemd/user/lariat.service`, `~/Library/LaunchAgents/dev.lariat.plist` | Startup folder: `lariat.cmd` |
| Logs | `journalctl --user -u lariat`, `~/Library/Logs/lariat.log` | the window it runs in |

Something off? [troubleshooting.md](troubleshooting.md).
