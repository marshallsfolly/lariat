# services.lariat: the NixOS module.
#
# `tailscale serve` (default) or nginx (your own certificate, on the listen
# addresses you give it) terminates TLS on the tailnet and proxies to the
# gateway on 127.0.0.1. The gateway runs as a systemd *user* unit for
# `services.lariat.user`, so terminals are that user's shells and it can reach
# per-user sockets (herdr, tmux). Authentication (passkeys) happens in the
# gateway. Nothing here has a host-specific default: user and host name must
# be set; secrets are optional (the gateway can keep its own).
self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib)
    mkOption
    mkEnableOption
    mkIf
    mkDefault
    types
    literalExpression
    optional
    optionalString
    concatMapStrings
    ;
  cfg = config.services.lariat;

  gateway = pkgs.callPackage ./gateway.nix { };
  herdrOn = cfg.herdr.package != null;
  nginxOn = cfg.frontend == "nginx";
  herdrMethods = pkgs.callPackage ./herdr-methods.nix { herdr = cfg.herdr.package; };
  wildcard = [
    "0.0.0.0"
    "[::]"
    "::"
    "*"
  ];
  listen = map (addr: {
    inherit addr;
    port = cfg.listenPort;
    ssl = true;
  }) cfg.listenAddresses;
  boolStr = b: if b then "1" else "0";

  # `lariat-enroll`: the enrollment link as a QR code, for the phone's camera.
  # The token rides in the URL fragment, which browsers never send to a server.
  # A token the gateway manages is renewed once used; one from your secrets is
  # only shown.
  cli = pkgs.callPackage ./lariat.nix {
    inherit gateway;
    web = cfg.webPackage;
  };
  enrollCommand = pkgs.writeShellScriptBin "lariat-enroll" ''
    export LARIAT_HOST=${lib.escapeShellArg cfg.hostName}
    export LARIAT_PORT=${toString cfg.backendPort}
    ${optionalString (cfg.secrets.enrollTokenFile != null) "export LARIAT_ENROLL_TOKEN_FILE=${lib.escapeShellArg cfg.secrets.enrollTokenFile}"}
    exec ${lib.getExe cli} enroll "$@"
  '';
  # `lariat`: the command line, pointed at this service (`lariat link`,
  # `lariat machines`, `lariat doctor`).
  cliCommand = pkgs.writeShellScriptBin "lariat" ''
    export LARIAT_HOST=${lib.escapeShellArg cfg.hostName}
    export LARIAT_PORT=${toString cfg.backendPort}
    export LARIAT_HTTPS_PORT=${toString (if nginxOn then cfg.listenPort else 443)}
    ${optionalString (cfg.tls.certFile != null) "export LARIAT_CERT_FILE=${lib.escapeShellArg (toString cfg.tls.certFile)}"}
    ${optionalString (cfg.tls.caFile != null) "export LARIAT_LINK_CA=${lib.escapeShellArg (toString cfg.tls.caFile)}"}
    exec ${lib.getExe cli} "$@"
  '';
  # `lariat-notify`: alerts from agent hooks on this machine (inbox and push).
  notifyCommand = pkgs.writeShellScriptBin "lariat-notify" ''
    export LARIAT_PORT=${toString cfg.backendPort}
    exec ${lib.getExe cli} notify "$@"
  '';
  claudeHook = event: {
    type = "command";
    command = "${lib.getExe notifyCommand} --hook claude ${event}";
    async = true;
    timeout = 10;
  };

  environment = lib.filterAttrs (_: v: v != null) (
    {
      LARIAT_HOST = cfg.hostName;
      LARIAT_PORT = toString cfg.backendPort;
      LARIAT_STATIC = "${cfg.webPackage}";
      LARIAT_SESSION_KEY_FILE = cfg.secrets.sessionKeyFile;
      LARIAT_ENROLL_TOKEN_FILE = cfg.secrets.enrollTokenFile;
      LARIAT_CREDENTIALS_FILE = cfg.secrets.credentialsFile;
      LARIAT_ENROLL = boolStr cfg.enrollment.enable;
      LARIAT_SESSION_IDLE_MINUTES = toString cfg.session.idleMinutes;
      LARIAT_SESSION_ABSOLUTE_MINUTES = toString cfg.session.absoluteMinutes;
      LARIAT_RP_NAME = cfg.webauthn.rpName;
      LARIAT_USER_NAME = cfg.webauthn.userName;
      LARIAT_APP_NAME = cfg.appName;
      LARIAT_SHELL = boolStr cfg.shell.enable;
      LARIAT_SHELL_COMMAND = if cfg.shell.command == null then null else builtins.toJSON cfg.shell.command;
      LARIAT_TMUX = if cfg.tmux.enable then "${cfg.tmux.package}/bin/tmux" else null;
      LARIAT_TMUX_DEFAULT = cfg.tmux.defaultSession;
      LARIAT_PROJECT_ROOTS = lib.concatStringsSep "," cfg.projectRoots;
      LARIAT_CERT_FILE = if cfg.tls.certFile == null then null else toString cfg.tls.certFile;
    }
    // lib.optionalAttrs herdrOn {
      LARIAT_HERDR = "${cfg.herdr.package}/bin/herdr";
      LARIAT_HERDR_SESSIONS = lib.concatStringsSep "," cfg.herdr.sessions;
      LARIAT_HERDR_METHODS = "${herdrMethods}";
      LARIAT_HERDR_PREFIX = cfg.herdr.prefixKey;
      LARIAT_AGENT_KINDS = lib.concatStringsSep "," cfg.herdr.agentKinds;
    }
    // lib.optionalAttrs (cfg.alerts.ntfy.url != null) {
      LARIAT_NTFY = cfg.alerts.ntfy.url;
      LARIAT_NTFY_TOPIC = cfg.alerts.ntfy.topic;
    }
    // lib.optionalAttrs cfg.dictation.enable {
      LARIAT_DICTATE = "1";
      LARIAT_DICTATE_SERVER = lib.getExe' cfg.dictation.package "whisper-server";
      LARIAT_DICTATE_MODEL = toString cfg.dictation.model;
      LARIAT_DICTATE_FFMPEG = "${cfg.dictation.ffmpegPackage}/bin/ffmpeg";
      LARIAT_DICTATE_LANGUAGE = cfg.dictation.language;
      LARIAT_DICTATE_THREADS = toString cfg.dictation.threads;
      LARIAT_DICTATE_MAX_SECONDS = toString cfg.dictation.maxSeconds;
      LARIAT_DICTATE_IDLE_MINUTES = toString cfg.dictation.idleMinutes;
      LARIAT_DICTATE_ARGS = builtins.toJSON cfg.dictation.extraArgs;
    }
    // {
      LARIAT_PUSH = boolStr cfg.push.enable;
    }
    // lib.optionalAttrs (cfg.push.subject != null) {
      LARIAT_PUSH_SUBJECT = cfg.push.subject;
    }
    // lib.optionalAttrs (cfg.dictation.enable && cfg.dictation.prompt != null) {
      LARIAT_DICTATE_PROMPT = cfg.dictation.prompt;
    }
    // cfg.extraEnvironment
  );

  fontsType = types.submodule {
    options = {
      family = mkOption {
        type = types.str;
        description = "Font family name (informational; the bundle serves it as \"lariat-mono\").";
      };
      regular = mkOption {
        type = types.either types.path types.str;
        description = "TTF/OTF file for the regular weight. Converted to WOFF2 at build time.";
      };
      bold = mkOption {
        type = types.nullOr (types.either types.path types.str);
        default = null;
        description = "TTF/OTF file for the bold weight.";
      };
    };
  };
in
{
  options.services.lariat = {
    enable = mkEnableOption "lariat, a passkey-gated PWA terminal and session dashboard";

    user = mkOption {
      type = types.str;
      example = "alice";
      description = "The account whose shells and sessions are served. The gateway runs as a user unit of this account.";
    };
    linger = mkOption {
      type = types.bool;
      default = true;
      description = "Enable lingering for `user`, so the gateway runs without a login session.";
    };

    frontend = mkOption {
      type = types.enum [
        "tailscale"
        "nginx"
      ];
      default = "tailscale";
      description = ''
        What serves HTTPS on the tailnet.

        `tailscale`: `tailscale serve` with the node's own ts.net certificate
        (publicly trusted, nothing to install on the phone). Needs
        `services.tailscale.enable` and HTTPS certificates turned on for the
        tailnet; the host name then appears in Certificate Transparency logs.

        `nginx`: nginx on `listenAddresses` with your own certificate
        (`tls.*`), e.g. from a private CA, for a name that is never logged.
      '';
    };
    hostName = mkOption {
      type = types.str;
      example = "host.example.ts.net";
      description = ''
        DNS name the phone uses: the WebAuthn RP ID and the origin host (with
        `frontend = "tailscale"`, this node's MagicDNS name).
        Passkeys are bound to it for good; changing it means enrolling again.
      '';
    };
    listenAddresses = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "100.64.0.10" ];
      description = "nginx frontend: addresses it listens on (IPv6 in brackets). Wildcards need `allowWildcardListen`.";
    };
    allowWildcardListen = mkOption {
      type = types.bool;
      default = false;
      description = "Allow 0.0.0.0/[::] in `listenAddresses`, for hosts that rely on firewall scoping alone.";
    };
    listenPort = mkOption {
      type = types.port;
      default = 443;
    };
    backendPort = mkOption {
      type = types.port;
      default = 8795;
      description = "Loopback port of the gateway.";
    };
    allowedCIDRs = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "100.64.0.0/10" ];
      description = "When non-empty, nginx allows only these client networks (`allow ...; deny all;`).";
    };
    firewall.interfaces = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = literalExpression "[ config.services.tailscale.interfaceName ]";
      description = "Interfaces on which `listenPort` is opened. The global allow list is never touched.";
    };
    nonlocalBind = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Set net.ipv{4,6}.ip_nonlocal_bind so nginx can bind an address that appears
        later (a Tailscale IP assigned after nginx starts).
      '';
    };
    nginx.after = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "tailscaled.service" ];
      description = "Units nginx should start after (and want).";
    };

    tls = {
      certFile = mkOption {
        type = types.nullOr (types.either types.path types.str);
        default = null;
        description = "PEM certificate (chain) for `hostName`. Required unless `tls.acmeHost` is set.";
      };
      keyFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "/run/secrets/lariat-tls-key";
        description = "Runtime path of the private key, readable by nginx. Never read at evaluation time.";
      };
      acmeHost = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Use the certificate of `security.acme.certs.<name>` instead of certFile/keyFile.";
      };
      caFile = mkOption {
        type = types.nullOr (types.either types.path types.str);
        default = null;
        description = ''
          The CA certificate that signed `certFile`, if it is a private CA. `lariat link`
          puts it in link codes so a hub can verify this machine. Without it, a private
          `certFile` itself is pinned (and a renewed certificate needs a new link).
        '';
      };
    };

    # All optional: without them the gateway keeps a session key and a one-time
    # enrollment token in its state dir, and the passkeys enrolled with it.
    secrets = {
      sessionKeyFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Runtime path of a random key (32+ bytes) that signs session cookies. Readable by `user`.";
      };
      enrollTokenFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Runtime path of the one-time enrollment token (16+ characters). Readable by `user`.";
      };
      credentialsFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = ''
          Runtime path of a passkey allowlist: a JSON list of credentials as the
          gateway logs them at enrollment. With it (and enrollment off) only these
          passkeys work, from your secrets manager. Readable by `user`.
        '';
      };
    };

    enrollment.enable = mkOption {
      type = types.bool;
      default = cfg.secrets.credentialsFile == null;
      defaultText = literalExpression "config.services.lariat.secrets.credentialsFile == null";
      description = ''
        Open /enroll so a new passkey can be registered with the enrollment token,
        and give `user` the `lariat-enroll` command (shows the link as a QR code).
        Turn off afterwards.
      '';
    };

    session = {
      idleMinutes = mkOption {
        type = types.ints.positive;
        default = 30;
      };
      absoluteMinutes = mkOption {
        type = types.ints.positive;
        default = 480;
      };
    };

    webauthn = {
      rpName = mkOption {
        type = types.str;
        default = "lariat";
        description = "Relying party name shown in the passkey sheet.";
      };
      userName = mkOption {
        type = types.str;
        default = "owner";
        description = "Account name stored with the passkey.";
      };
    };

    appName = mkOption {
      type = types.str;
      default = "lariat";
      description = "App name (manifest, page title).";
    };
    shortName = mkOption {
      type = types.str;
      default = cfg.appName;
      defaultText = literalExpression "config.services.lariat.appName";
      description = "Home-screen label.";
    };

    shell = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "Offer a plain shell (a fresh one per terminal; nothing persists).";
      };
      command = mkOption {
        type = types.nullOr (types.listOf types.str);
        default = null;
        example = [
          "/run/current-system/sw/bin/zsh"
          "-l"
        ];
        description = "argv of the shell. null = the user's login shell with -l.";
      };
    };

    tmux = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "List, attach, create, rename and kill tmux sessions.";
      };
      package = lib.mkPackageOption pkgs "tmux" { };
      defaultSession = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Session opened (or created) by the tmux quick button.";
      };
    };

    herdr = {
      package = mkOption {
        type = types.nullOr types.package;
        default = null;
        description = "The herdr package. null disables the herdr dashboard entirely.";
      };
      sessions = mkOption {
        type = types.listOf types.str;
        default = [ "default" ];
        description = "herdr sessions always listed; running named sessions are discovered too.";
      };
      agentKinds = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [
          "claude"
          "codex"
        ];
        description = "Agent kinds offered under New agent. Empty hides it.";
      };
      prefixKey = mkOption {
        type = types.str;
        default = "ctrl+b";
        description = "herdr's prefix key, sent by the key bar's prefix button.";
      };
    };

    projectRoots = mkOption {
      type = types.listOf types.str;
      default = [ "~" ];
      example = [ "~/dev" ];
      description = "Directories (and their children) allowed as the working directory of new sessions and agents.";
    };

    alerts.ntfy = {
      url = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "http://127.0.0.1:2586";
        description = "Base URL of an ntfy server whose topic feeds the in-app inbox. null disables it.";
      };
      topic = mkOption {
        type = types.str;
        default = "agents";
      };
    };

    notify.package = mkOption {
      type = types.package;
      readOnly = true;
      default = notifyCommand;
      defaultText = literalExpression "lariat-notify";
      description = "The `lariat-notify` command, for hooks of other agents (`--hook <agent> stop|notification`).";
    };
    notify.claudeCode = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Claude Code hooks (a system managed-settings drop-in) that send "needs
        you" and "finished" to lariat (the inbox and push notifications, for
        sessions outside herdr too), and a rule that lets agents read the images
        sent from the phone. `lariat-notify` is installed either way.
      '';
    };

    push = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Web Push: devices that turn notifications on (in the app's menu) get
          agent alerts while the app is closed. Pushes go to the device's push
          service (Apple's for an iPhone), end-to-end encrypted. Nothing is sent
          until a device subscribes.
        '';
      };
      subject = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "mailto:you@example.com";
        description = "VAPID contact the push service may use. Default: https://<hostName>.";
      };
    };

    dictation = {
      enable = mkEnableOption ''
        the dictation button. Audio is transcribed on this host by whisper.cpp's
        whisper-server, which the gateway starts on first use and keeps loaded
        (partial text while you talk, the final text right after)'';
      package = lib.mkPackageOption pkgs "whisper-cpp" {
        extraDescription = "Must provide `bin/whisper-server`. Use whisper-cpp-vulkan (or a CUDA build) for a GPU.";
      };
      model = mkOption {
        type = types.nullOr types.path;
        default = null;
        example = literalExpression ''
          pkgs.fetchurl {
            url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin";
            hash = "sha256-...";
          }
        '';
        description = "A ggml whisper model. Required when dictation is on.";
      };
      language = mkOption {
        type = types.str;
        default = "en";
      };
      threads = mkOption {
        type = types.ints.positive;
        default = 4;
      };
      maxSeconds = mkOption {
        type = types.ints.positive;
        default = 60;
      };
      idleMinutes = mkOption {
        type = types.numbers.nonnegative;
        default = 0;
        description = ''
          Stop whisper-server (freeing its memory) after this long without dictation.
          0 keeps it loaded from boot, so the first word is as fast as the rest.
        '';
      };
      prompt = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "Claude, tmux, NixOS, git rebase, pull request.";
        description = "Initial prompt for whisper: words and spellings to expect.";
      };
      extraArgs = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [ "--device" "1" ];
        description = "Extra whisper-server arguments.";
      };
      ffmpegPackage = lib.mkPackageOption pkgs "ffmpeg-headless" {
        extraDescription = "Only decodes uploaded recordings, from browsers without AudioWorklet.";
      };
    };

    theme = {
      palette = mkOption {
        type = types.attrsOf types.anything;
        default = import ./themes/default.nix;
        defaultText = literalExpression "import \"\${lariat}/nix/themes/default.nix\"";
        description = "Colour palette; keys in nix/themes/schema.md. Extra keys and non-string values are ignored.";
      };
      fonts = mkOption {
        type = types.nullOr fontsType;
        default = null;
        description = "Terminal/UI font (a Nerd Font Mono). null = JetBrainsMono Nerd Font Mono.";
      };
    };

    webPackage = mkOption {
      type = types.package;
      default = self.lib.mkWeb pkgs {
        theme = cfg.theme.palette;
        inherit (cfg.theme) fonts;
        inherit (cfg) appName shortName;
      };
      defaultText = literalExpression "lariat.lib.mkWeb pkgs { theme = cfg.theme.palette; ... }";
      description = "The static PWA bundle. Override for a custom frontend.";
    };

    extraEnvironment = mkOption {
      type = types.attrsOf types.str;
      default = { };
      example = {
        SHELL = "/run/current-system/sw/bin/bash";
      };
      description = "Extra environment for the gateway (and so for the shells it starts).";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.hasInfix "." cfg.hostName;
        message = "services.lariat.hostName must be a domain name (WebAuthn does not accept bare host names or IPs).";
      }
      {
        assertion = !nginxOn || cfg.listenAddresses != [ ];
        message = "services.lariat.listenAddresses must not be empty with frontend = \"nginx\".";
      }
      {
        assertion = cfg.allowWildcardListen || !(lib.any (a: lib.elem a wildcard) cfg.listenAddresses);
        message = "services.lariat.listenAddresses contains a wildcard; set allowWildcardListen = true if that is intended.";
      }
      {
        assertion = !nginxOn || cfg.tls.acmeHost != null || (cfg.tls.certFile != null && cfg.tls.keyFile != null);
        message = "services.lariat needs tls.certFile and tls.keyFile, or tls.acmeHost, with frontend = \"nginx\".";
      }
      {
        assertion = nginxOn || config.services.tailscale.enable;
        message = "services.lariat.frontend = \"tailscale\" needs services.tailscale.enable = true.";
      }
      {
        assertion = !cfg.dictation.enable || cfg.dictation.model != null;
        message = "services.lariat.dictation.model must be set when dictation is enabled.";
      }
      {
        assertion = cfg.backendPort != cfg.listenPort;
        message = "services.lariat.backendPort must differ from listenPort.";
      }
    ];

    services.nginx = mkIf nginxOn {
      enable = true;
      recommendedTlsSettings = mkDefault true;
      recommendedOptimisation = mkDefault true;
      recommendedGzipSettings = mkDefault true;
      recommendedProxySettings = mkDefault true;

      virtualHosts.${cfg.hostName} = {
        onlySSL = true;
        inherit listen;
        sslCertificate = mkIf (cfg.tls.acmeHost == null) cfg.tls.certFile;
        sslCertificateKey = mkIf (cfg.tls.acmeHost == null) cfg.tls.keyFile;
        useACMEHost = cfg.tls.acmeHost;
        extraConfig = ''
          ${concatMapStrings (cidr: "allow ${cidr};\n") cfg.allowedCIDRs}${optionalString (cfg.allowedCIDRs != [ ]) "deny all;\n"}
          add_header Strict-Transport-Security "max-age=31536000" always;
          client_max_body_size 21m; # images from the phone
        '';
        locations."/" = {
          proxyPass = "http://127.0.0.1:${toString cfg.backendPort}";
          proxyWebsockets = true;
          extraConfig = ''
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_read_timeout 1h;
            proxy_send_timeout 1h;
          '';
        };
      };

      # Anything that is not our host name on these addresses: no certificate,
      # the TLS handshake is refused; a wrong Host header gets 444.
      virtualHosts."lariat-reject" = {
        default = true;
        rejectSSL = true;
        inherit listen;
        locations."/".return = "444";
      };
    };

    # `tailscale serve` keeps its config in tailscaled; this sets it at boot and
    # removes it when the unit stops (lariat disabled or switched to nginx).
    systemd.services.lariat-tailscale-serve = mkIf (!nginxOn) {
      description = "Serve lariat on the tailnet (tailscale serve)";
      after = [ "tailscaled.service" ];
      wants = [ "tailscaled.service" ];
      wantedBy = [ "multi-user.target" ];
      path = [ config.services.tailscale.package ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      script = ''
        for _ in $(seq 60); do tailscale status --json >/dev/null 2>&1 && break; sleep 1; done
        tailscale serve --bg --yes --https=${toString cfg.listenPort} http://127.0.0.1:${toString cfg.backendPort}
      '';
      preStop = "tailscale serve --https=${toString cfg.listenPort} off || true";
    };

    systemd.services.nginx = mkIf (nginxOn && cfg.nginx.after != [ ]) {
      after = cfg.nginx.after;
      wants = cfg.nginx.after;
    };

    boot.kernel.sysctl = mkIf (nginxOn && cfg.nonlocalBind) {
      "net.ipv4.ip_nonlocal_bind" = 1;
      "net.ipv6.ip_nonlocal_bind" = 1;
    };

    networking.firewall.interfaces = lib.genAttrs (lib.optionals nginxOn cfg.firewall.interfaces) (_: {
      allowedTCPPorts = [ cfg.listenPort ];
    });

    users.users.${cfg.user} = {
      linger = mkIf cfg.linger true;
      packages = [
        cliCommand
        notifyCommand
      ]
      ++ optional cfg.enrollment.enable enrollCommand;
    };

    environment.etc."claude-code/managed-settings.d/50-lariat.json" = mkIf cfg.notify.claudeCode {
      text = builtins.toJSON {
        hooks = {
          Notification = [ { hooks = [ (claudeHook "notification") ]; } ];
          Stop = [ { hooks = [ (claudeHook "stop") ]; } ];
        };
        # Images from the phone land here; agents read them without asking.
        permissions.allow = [ "Read(~/.local/state/lariat/uploads/**)" ];
      };
    };

    systemd.user.services.lariat = {
      description = "lariat gateway (passkey PWA terminal)";
      wantedBy = [ "default.target" ];
      after = optional herdrOn "herdr.service";
      wants = optional herdrOn "herdr.service";
      unitConfig.ConditionUser = cfg.user;
      # Inherit the user manager's PATH instead of NixOS' minimal service PATH:
      # shells (and any tmux server) started from here should look like a login.
      enableDefaultPath = false;
      inherit environment;
      serviceConfig = {
        ExecStart = lib.getExe gateway;
        Restart = "on-failure";
        RestartSec = 2;
        StateDirectory = "lariat";
        StateDirectoryMode = "0700";
        RuntimeDirectory = "lariat";
        RuntimeDirectoryMode = "0700";
        # Terminals are children of this unit: stopping it ends attach clients and
        # plain shells, never herdr panes or tmux sessions (separate servers).
        KillMode = "control-group";
        TimeoutStopSec = 10;
      };
    };
  };
}
