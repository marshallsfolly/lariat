# Troubleshooting

Start with `lariat doctor`: it checks Tailscale, HTTPS, the gateway, enrolled
devices, notifications, dictation and hooks, and prints the fix for anything
missing.

## Reaching it

**The phone says the page can't be opened.** The Tailscale app on the phone
must be connected to the same tailnet. Check the name with `lariat doctor`
(MagicDNS must be on).

**"tailscale serve needs permission"** (Linux): run
`sudo tailscale set --operator=$USER` once. Windows: run the command lariat
prints once in an administrator PowerShell.

**`lariat doctor` says the service is installed but lariat is not
running.** Read its log (`journalctl --user -u lariat`, macOS
`~/Library/Logs/lariat.log`). On Linux it is nearly always the permission
above: `sudo tailscale set --operator=$USER`, then
`systemctl --user restart lariat`.

**"No HTTPS yet"**: turn on HTTPS certificates at the
[DNS page](https://login.tailscale.com/admin/dns), or leave them off and let
lariat make its own CA when it starts ([tls.md](tls.md#your-own-ca)).

**I turned Tailscale's certificates on, but the phone still warns.** lariat
switches to them when it starts: restart it (`systemctl --user restart
lariat`, or run `lariat service install` again). If you made your own CA on
purpose (`lariat tls init`), lariat keeps it; delete the `tls` folder in its
state directory to go back to Tailscale's.

**The CA page (`lariat tls phone`) does not open on the phone.** It listens
on port 8411 of the machine's Tailscale address, over plain HTTP; a firewall
on the machine (Windows Defender, macOS, ufw) may block it. Allow it for the
minute you need it, or AirDrop / mail yourself `ca.pem` from the `tls` folder.

**Port 8795 is taken**: `lariat serve --port 8796` (and
`lariat service install -- --port 8796`).

## Passkeys

**The passkey prompt never appears, or Safari shows a certificate warning.**
The certificate is not trusted. With your own CA on iPhone, the profile must be
installed *and* full trust turned on (Settings → General → About → Certificate
Trust Settings). With Tailscale's certificate, the first visit after turning
HTTPS on can take a few seconds while it is issued.

**"Enrollment is closed; that code was used already."** Codes work once:
`lariat enroll` makes a new one.

**The installed app asks to Unlock although I just enrolled.** Home Screen apps
keep their own cookies; tap Unlock once. The passkey is the same.

**I renamed the machine.** Passkeys are bound to the old name: enroll again
(`lariat enroll`), and with your own CA run `lariat tls init` afresh after
deleting `~/.local/state/lariat/tls`.

## Notifications

**No "Push notifications" switch, or it says to add the app to the Home
Screen.** On iPhone, push works only in the installed app (iOS 16.4+), not in
Safari.

**"Send a test" works but agents never notify.** herdr agents notify on their
own. Claude Code needs `lariat hooks claude --install` (then start a new
session). Notifications are skipped while the app is open in front: the alert
shows inside the app instead.

**One alert did not arrive.** lariat logs each decision:
`journalctl --user -u lariat | grep push:` (macOS: `~/Library/Logs/lariat.log`)
says "sent", or why not: the app was open on the phone (it counts as open for
up to 15 s after the last sign of life, if the phone locked without saying
so), the same alert had just gone out, or no device wants that kind.

**Notifications stopped.** iOS drops a subscription when the app is deleted or
notifications are turned off in Settings. Turn Push notifications off and on
again in the app.

## Images

**The agent asks for permission to read the image.** Run
`lariat hooks claude --install` (NixOS: `services.lariat.notify.claudeCode`);
it allows reading lariat's uploads folder. Or answer "always allow" once.

**"That image is too large."** Images are shrunk on the phone first; the limit
is 20 MB after that. Very large panoramas may still exceed it.

**The image key does nothing.** The phone needs to allow the app to use
Photos or the camera; iOS asks the first time.

## Dictation

**The mic does nothing or "Microphone permission denied".** Allow the
microphone for the app (iOS: Settings → Apps → Safari → Microphone).

**iOS asks for the microphone again.** iOS keeps a home-screen app's
microphone permission only until the app is closed, so expect one prompt per
launch. It may
stop asking if you also allow it in Safari: open the same address in Safari,
then aA → Website Settings → Microphone → Allow (iOS does not promise this
carries over to the home-screen app).

**The first words are slow.** The model loads when lariat starts; give it a
few seconds after a restart. A GPU build of whisper.cpp makes the rest fast.

**It sends too early or too late.** In a terminal's ⋮ menu, change "Stop when
I pause" (0.6 to 2.5 s, or off) and whether it presses Enter.

## Selecting and copying

**I can't select text in the terminal.** Hold the terminal (or ⋮ → Select
text): its text opens as plain text, where the phone's own selection works
(hold a word, drag the handles, Copy), or tap **All**. Agents and tmux show
their whole recent history there, not only the screen. To paste, use the
paste key in the key bar.

## Logs

- `journalctl --user -u lariat -f` (Linux service), `~/Library/Logs/lariat.log`
  (macOS service), or the terminal running `lariat`.
- Each recording logs its timings, never audio or text.
- Sessions live in memory: restarting lariat logs every device out; Unlock again.
