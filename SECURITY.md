# Security

lariat hands a shell on your machine to whoever unlocks it. Treat it like
SSH: one owner, a private network, and never the public internet.

## Threat model

**Protected against**

- Anyone who can reach the port but has no enrolled passkey: they get the
  static app shell and the login button, nothing else. All data and every
  action require a session; sessions require a passkey assertion with user
  verification, for this exact origin and RP ID.
- Cross-site requests and cross-site WebSocket hijacking: every POST checks
  `Origin` and a custom header (which forces a CORS preflight that is never
  granted); the WebSocket upgrade checks `Origin`. Cookies are `__Host-`,
  Secure, HttpOnly, SameSite=Strict.
- Replay: login and registration challenges are random, single-use and expire
  after 120 seconds. Authenticator sign counters are checked when non-zero.
- Enrollment abuse: enrollment needs a one-time token compared in constant
  time and burned (persistently) on first use. Without a credentials secret
  the token lives in the user's state dir and `lariat enroll` renews it, so
  only someone who can read your files can enroll a device.
  `lariat enroll` (`lariat-enroll` on NixOS) shows it as a QR code (in the URL fragment, never sent to
  the server or logged); anyone who sees that code before you scan it could
  enroll first, and the gateway logs every enrollment.
- Command injection: terminals run a fixed argv per target kind; names and IDs
  are validated by pattern and against live state. The herdr integration
  calls a fixed set of methods with validated parameters and never forwards
  client JSON.
- Content injection: a strict Content-Security-Policy (scripts, fonts, images
  and connections from the same origin only). Styles allow `'unsafe-inline'`
  only because xterm.js creates `<style>` elements at runtime.
- Brute force: auth endpoints are rate-limited per client (10/min) with a
  global 5-minute lockout after 20 failures.

**Not protected against**

- A compromised or stolen *unlocked* device, or a compromised passkey
  provider (e.g. your iCloud account): a session is a shell.
- Anyone who can already run code as your user on the host.
- Exposure beyond a private network. Nothing in lariat is designed to face
  the internet: never put it behind `tailscale funnel` or a public proxy. The
  nginx frontend refuses wildcard listen addresses unless you opt in.
- Other users of a shared VPN reaching the login page. Restrict the port with
  your VPN's ACLs (for Tailscale: only your own devices to `host:443`).

**Operational notes**

- With the nginx frontend the TLS key belongs to nginx; the gateway (which
  spawns shells) never reads it. With `tailscale serve`, tailscaled holds the
  ts.net certificate, and the host name is in public CT logs.
- Web Push: alerts go to the device's push service (Apple, Google or Mozilla)
  encrypted end to end (RFC 8291); the service sees size and timing. Only
  devices whose passkey is still allowed get them, only known push services
  are contacted, and nothing is sent until a device turns notifications on.
  The VAPID key and subscriptions live in the state dir (0600).
- If you sign the certificate with your own CA, that root is trusted by your
  phone for every domain unless it is name-constrained. Keep its key offline
  or encrypted, or use a name-constrained intermediate.
- A gateway restart logs everyone out (sessions are in memory).
- `lariat tls init` keeps the CA key in the state dir (0600). The CA is
  name-constrained to one host name (critical X.509 NameConstraints), so a
  phone that trusts it trusts nothing else; still, delete or move the CA key
  if you will not renew certificates on this machine.
- Images from the phone: only an authenticated session can upload; only image
  files are accepted (checked by content), at most 20 MB, saved under
  `<state>/uploads` with names lariat chooses (0600) and deleted after 7 days.
  The phone re-encodes them first, which strips metadata such as GPS. They are
  never served back over the web.
- Links between machines ([docs/machines.md](docs/machines.md)): a link token
  is a bearer credential with a session's powers on that machine (a shell),
  but it cannot sign in, enroll or touch push. The machine keeps only its
  SHA-256 hash and checks it in constant time; the hub keeps the token in
  `<state>/machines.json` (0600) and sends it only to that machine's URL over
  HTTPS, verified against Tailscale's public certificate, the machine's own CA
  or its pinned certificate (carried in the link code). Revoking it
  (`lariat link --revoke`) closes the hub's open terminals within seconds.
  A hub that is compromised reaches every machine linked to it.
- Updates (release binaries): lariat trusts this repository's GitHub
  releases. It fetches over HTTPS from github.com, checks the binary against
  the release's `SHA256SUMS`, runs its `--version`, and only then swaps it in.
  The checksum guards against a damaged download, not against someone who
  controls the release: whoever can publish a release here can run code on
  every machine with automatic updates. If that is not a trust you want, use
  `lariat update --mode notify` (or `off`) and update by hand, or build from
  source with Nix (Nix installs never self-update). Anyone with a session can
  start an update or change the mode, but only to this repository's latest
  release.
- `lariat tls phone` serves only the public CA certificate, over plain HTTP,
  bound to the Tailscale IPv4 address, until you stop it.
- Dictation audio is held in the gateway's memory for one recording and sent
  only to whisper-server on 127.0.0.1 (uploads from older browsers pass
  through a private temporary directory). Audio and text are never logged.
  While whisper-server runs, other local users can reach its loopback port;
  on a shared machine leave dictation off. With "send" on, a pause presses
  Enter on whatever was heard: turn it off for shells where that matters.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting ("Report a vulnerability"
under the repository's Security tab). Do not open a public issue for security
problems.
