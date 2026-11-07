# NixOS and home-manager

## NixOS

Add the input:

```nix
inputs.lariat = {
  url = "github:marshallsfolly/lariat";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

### Minimal

```nix
{ inputs, ... }:
{
  imports = [ inputs.lariat.nixosModules.default ];
  services.tailscale.enable = true;
  services.lariat = {
    enable = true;
    user = "alice";                            # whose shells and agents
    hostName = "myhost.your-tailnet.ts.net";   # this machine's MagicDNS name
  };
}
```

This serves lariat with `tailscale serve` and Tailscale's certificate (turn
on HTTPS certificates in the admin console), runs the gateway as a systemd
user unit for `alice` (lingering, so it runs without a login) and keeps its
secrets in `~alice/.local/state/lariat`. After deploying, as `alice`:

```sh
lariat-enroll     # QR code for the phone
```

then do the phone steps from [Getting started, step 5](setup.md#5-enroll-your-phone).

The module also gives `alice` a `lariat` command pointed at this service
(`lariat doctor`, and `lariat link` / `lariat machines` for
[several machines](machines.md)). With the nginx frontend and a private CA,
set `tls.caFile` to that CA's certificate so link codes carry it.

To keep the host name out of Certificate Transparency logs, use your own CA
with the nginx frontend: [tls.md](tls.md#your-own-ca-on-nixos).

### Agents, notifications and dictation

```nix
services.lariat = {
  # ...
  herdr = {
    package = inputs.llm-agents.packages.${pkgs.stdenv.hostPlatform.system}.herdr;  # github:numtide/llm-agents.nix
    agentKinds = [ "claude" "codex" ];
  };
  tmux.defaultSession = "main";
  projectRoots = [ "~/code" ];

  notify.claudeCode = true;   # Claude Code hooks (managed settings) report to lariat

  dictation = {
    enable = true;
    package = pkgs.whisper-cpp-vulkan;   # or a CUDA build; pkgs.whisper-cpp for CPU
    model = pkgs.fetchurl {
      url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en-q8_0.bin";
      hash = "sha256-Z6F59gjqYRS9P9uQYOditYij+zvQDEOHlxvk0XeVgGc=";
    };
  };
};
```

- herdr agents raise "needs you" and "finished" alerts on their own.
- `notify.claudeCode` adds Claude Code hooks for every session on the machine
  (in herdr or not), and lets agents read the images sent from the phone
  (`~/.local/state/lariat/uploads`) without asking. For other agents, call
  `${config.services.lariat.notify.package}/bin/lariat-notify --hook <agent> stop`
  from their hooks.
- Dictation keeps the model loaded on the GPU from boot
  (`dictation.idleMinutes = 0`), so the first word is as fast as the rest.

[examples/agents-host.nix](../examples/agents-host.nix) is a complete host.

### Passkeys from your secrets manager

By default the gateway keeps enrolled passkeys in its state directory and
enrollment stays available through one-time codes. To pin the passkeys in
your configuration instead:

1. Enroll as usual. The gateway logs each new passkey as one JSON line:
   `journalctl --user -u lariat | grep -A1 enrolled`.
2. Put those objects, as a JSON list, in a secret readable by the user, e.g.
   sops-nix `lariat/credentials`.
3. Set `secrets.credentialsFile` to its path. Enrollment then turns off by
   default (`enrollment.enable` follows `credentialsFile == null`), `/enroll`
   is a 404, and only the listed passkeys work.

`secrets.sessionKeyFile` and `secrets.enrollTokenFile` can come from the
secrets manager too.

### All options

All under `services.lariat`; only `enable`, `user` and `hostName` are required.

| Option | Default | Notes |
|---|---|---|
| `user` | required | Account whose shells are served; the gateway is its user unit. |
| `hostName` | required | The name the phone uses: WebAuthn RP ID and origin. Passkeys are bound to it. |
| `frontend` | `"tailscale"` | `"tailscale"` (`tailscale serve`, needs `services.tailscale.enable`) or `"nginx"`. |
| `listenAddresses` | `[ ]` | nginx: listen IPs; wildcards need `allowWildcardListen`. |
| `listenPort` / `backendPort` | `443` / `8795` | The gateway binds 127.0.0.1 only. |
| `allowedCIDRs` / `firewall.interfaces` | `[ ]` | nginx: allowed client networks; interfaces that get `listenPort`. |
| `nonlocalBind` / `nginx.after` | `false` / `[ ]` | nginx: bind an address that appears after boot (start after `tailscaled.service`). |
| `tls.certFile` / `tls.keyFile` / `tls.acmeHost` | `null` | nginx: your certificate. |
| `tls.caFile` | `null` | The private CA that signed `certFile`, for `lariat link` codes ([several machines](machines.md)). |
| `secrets.sessionKeyFile` / `enrollTokenFile` / `credentialsFile` | `null` | Optional; without them the gateway keeps its own in its state dir. |
| `enrollment.enable` | `credentialsFile == null` | With a credentials secret, turn it off to allow only those passkeys. |
| `push.enable` / `push.subject` | `true` / `null` | Web Push; nothing is sent until a device turns it on. |
| `notify.claudeCode` | `false` | Claude Code hooks (managed settings) that report to lariat; `lariat-notify` is installed either way. |
| `session.idleMinutes` / `absoluteMinutes` | `30` / `480` | |
| `appName` / `shortName` | `"lariat"` | Home-screen label. |
| `shell.enable` / `shell.command` | `true` / `null` | `null` = login shell. |
| `tmux.enable` / `tmux.defaultSession` | `true` / `null` | |
| `herdr.package` / `sessions` / `agentKinds` / `prefixKey` | `null` / `[ "default" ]` / `[ ]` / `"ctrl+b"` | `null` disables herdr. |
| `projectRoots` | `[ "~" ]` | Where new sessions and agents may start. |
| `alerts.ntfy.url` / `topic` | `null` / `"agents"` | |
| `dictation.enable` / `package` / `model` | `false` / `pkgs.whisper-cpp` / `null` | Use `whisper-cpp-vulkan` (or a CUDA build) for speed. |
| `dictation.idleMinutes` / `prompt` / `extraArgs` | `0` / `null` / `[ ]` | 0 keeps the model loaded from boot; whisper's initial prompt; extra `whisper-server` flags. |
| `dictation.language` / `threads` / `maxSeconds` | `"en"` / `4` / `60` | |
| `theme.palette` / `theme.fonts` | "lariat dark" / JetBrains Mono | Any palette ([schema](../nix/themes/schema.md)) and Nerd Font. |
| `extraEnvironment` | `{ }` | Extra gateway (and shell) environment. |


## home-manager

For Linux or macOS with home-manager (NixOS or not):

```nix
{ inputs, ... }:
{
  imports = [ inputs.lariat.homeManagerModules.default ];
  services.lariat = {
    enable = true;
    name = "devbox";        # the home-screen label
    hooks.claude = true;    # Claude Code sessions alert the phone (merged on each switch; yours kept)
    # hooks.grok = true;
    # dictation = true;     # whisper.cpp (CPU) and the base.en model
    # environment.LARIAT_DICTATE_MODEL = "/path/to/model.bin";
  };
}
```

It runs `lariat serve` as a systemd user service (Linux; add
`loginctl enable-linger $USER`) or a launchd agent (macOS, logs in
`~/Library/Logs/lariat.log`). The `lariat` command is installed too:
`lariat enroll`, `lariat join`, `lariat tls init`, `lariat doctor`. To add
the machine to your hub, see [several machines](machines.md).
