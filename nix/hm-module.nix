# services.lariat for home-manager: run `lariat serve` as a systemd user
# service (Linux) or a launchd agent (macOS) on a machine that is on a tailnet. It finds the
# node's MagicDNS name and sets up `tailscale serve` itself; `lariat enroll`
# shows the QR code for a phone.
self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib) mkOption mkEnableOption types mkIf;
  cfg = config.services.lariat;
  packages = self.packages.${pkgs.stdenv.hostPlatform.system};
  argList = (
    [
      "--port"
      (toString cfg.port)
      "--https-port"
      (toString cfg.httpsPort)
      "--name"
      cfg.name
    ]
    ++ lib.optionals (cfg.host != null) [
      "--host"
      cfg.host
    ]
    ++ lib.optional (!cfg.tailscaleServe) "--no-tailscale-serve"
  );
  args = lib.escapeShellArgs argList;
in
{
  options.services.lariat = {
    enable = mkEnableOption "lariat, a passkey-gated PWA terminal for your phone, on your tailnet";
    package = mkOption {
      type = types.package;
      default = if cfg.dictation then packages.lariat-dictation else packages.lariat;
      defaultText = lib.literalExpression "lariat.packages.\${system}.lariat (or lariat-dictation)";
    };
    dictation = mkOption {
      type = types.bool;
      default = false;
      description = "Bundle whisper.cpp (CPU) and the base.en model for the mic button. Use a GPU build for speed.";
    };
    name = mkOption {
      type = types.str;
      default = "lariat";
      description = "App name on the phone's home screen.";
    };
    host = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "DNS name the phone uses; null = this node's MagicDNS name.";
    };
    port = mkOption {
      type = types.port;
      default = 8795;
      description = "Loopback port of the gateway.";
    };
    httpsPort = mkOption {
      type = types.port;
      default = 443;
      description = "Tailnet HTTPS port (tailscale serve).";
    };
    tailscaleServe = mkOption {
      type = types.bool;
      default = true;
      description = "Configure `tailscale serve` (needs `sudo tailscale set --operator=$USER` once).";
    };
    hooks = {
      claude = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Claude Code sessions alert the phone: on every switch, merge lariat's hooks
          (and the read permission for images from the phone) into ~/.claude/settings.json.
          Your other settings are kept.
        '';
      };
      grok = mkOption {
        type = types.bool;
        default = false;
        description = "Grok Build sessions alert the phone (~/.grok/hooks/lariat.json).";
      };
    };
    environment = mkOption {
      type = types.attrsOf types.str;
      default = { };
      example = {
        LARIAT_AGENT_KINDS = "claude,codex";
      };
      description = "Extra gateway environment (LARIAT_* settings, see the README).";
    };
  };

  config = mkIf cfg.enable {
    home.packages = [ cfg.package ];
    # The hooks run the profile's lariat, a path that survives updates.
    home.activation.lariatHooks = mkIf (cfg.hooks.claude || cfg.hooks.grok) (
      lib.hm.dag.entryAfter [ "writeBoundary" ] ''
        ${lib.optionalString cfg.hooks.claude "run ${lib.getExe cfg.package} hooks claude --install --command ${config.home.profileDirectory}/bin/lariat"}
        ${lib.optionalString cfg.hooks.grok "run ${lib.getExe cfg.package} hooks grok --install --command ${config.home.profileDirectory}/bin/lariat"}
      ''
    );
    # So `lariat enroll`, `link` and `notify` in a shell find this service.
    home.sessionVariables = {
      LARIAT_PORT = toString cfg.port;
      LARIAT_HTTPS_PORT = toString cfg.httpsPort;
    }
    // lib.optionalAttrs (cfg.host != null) { LARIAT_HOST = cfg.host; };

    launchd.agents.lariat = mkIf pkgs.stdenv.hostPlatform.isDarwin {
      enable = true;
      config = {
        ProgramArguments = [
          (lib.getExe cfg.package)
          "serve"
        ] ++ argList;
        EnvironmentVariables = {
          # The Tailscale app's CLI is found by lariat itself; this is for the shells.
          PATH = "${config.home.profileDirectory}/bin:/run/current-system/sw/bin:/nix/var/nix/profiles/default/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin";
        } // cfg.environment;
        RunAtLoad = true;
        KeepAlive = true;
        StandardOutPath = "${config.home.homeDirectory}/Library/Logs/lariat.log";
        StandardErrorPath = "${config.home.homeDirectory}/Library/Logs/lariat.log";
      };
    };

    systemd.user.services.lariat = mkIf pkgs.stdenv.hostPlatform.isLinux {
      Unit = {
        Description = "lariat: passkey PWA terminal on the tailnet";
        After = [ "network-online.target" ];
      };
      Service = {
        ExecStart = "${lib.getExe cfg.package} serve ${args}";
        # Shells started from here should find the user's tools.
        Environment = [
          "PATH=${config.home.profileDirectory}/bin:/run/current-system/sw/bin:/usr/local/bin:/usr/bin:/bin"
        ] ++ lib.mapAttrsToList (k: v: "${k}=${v}") cfg.environment;
        Restart = "on-failure";
        RestartSec = 5;
        KillMode = "control-group";
        TimeoutStopSec = 10;
      };
      Install.WantedBy = [ "default.target" ];
    };
  };
}
