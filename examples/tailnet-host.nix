# A NixOS host on a Tailscale tailnet served by nginx with a certificate from
# your own CA, so the host name never reaches Certificate Transparency logs
# (or see acme-host.nix). For the simple path, see agents-host.nix.
# Import from a flake that has `inputs.lariat`.
{
  inputs,
  config,
  pkgs,
  ...
}:
{
  imports = [ inputs.lariat.nixosModules.default ];

  services.lariat = {
    enable = true;
    frontend = "nginx";
    user = "alice";
    hostName = "myhost.example.ts.net";
    listenAddresses = [ "100.64.0.10" ]; # example; use your `tailscale ip -4`
    nonlocalBind = true; # nginx may start before tailscaled has the IP
    nginx.after = [ "tailscaled.service" ];
    allowedCIDRs = [ "100.64.0.0/10" ];
    firewall.interfaces = [ config.services.tailscale.interfaceName ];

    tls.certFile = ./certs/myhost.pem;
    tls.keyFile = "/run/secrets/lariat-tls-key"; # readable by nginx only
    secrets = {
      sessionKeyFile = "/run/secrets/lariat-session-key";
      enrollTokenFile = "/run/secrets/lariat-enroll-token";
      credentialsFile = "/run/secrets/lariat-credentials"; # JSON list, "[]" before enrolling
    };
    enrollment.enable = true; # turn off after enrolling your passkey

    tmux.defaultSession = "main";
    # Optional integrations:
    # herdr.package = pkgs.herdr;                     # if you use herdr
    # herdr.agentKinds = [ "claude" "codex" ];
    # alerts.ntfy.url = "http://127.0.0.1:2586";
    # dictation = {
    #   enable = true;
    #   model = pkgs.fetchurl {
    #     url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin";
    #     hash = "sha256-…";
    #   };
    # };
    # theme.palette = import ./my-palette.nix;
  };
}
