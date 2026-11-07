# Same idea with a publicly trusted certificate from Let's Encrypt via a DNS-01
# challenge (no port 80 needed, the host can stay private). Note that public
# certificates put the host name in Certificate Transparency logs.
{
  inputs,
  config,
  ...
}:
{
  imports = [ inputs.lariat.nixosModules.default ];

  security.acme = {
    acceptTerms = true;
    defaults.email = "admin@example.com";
    certs."term.example.com" = {
      dnsProvider = "cloudflare"; # any lego DNS provider
      environmentFile = "/run/secrets/acme-dns-credentials";
      group = "nginx";
    };
  };

  services.lariat = {
    enable = true;
    frontend = "nginx";
    user = "alice";
    hostName = "term.example.com"; # resolves to a private address, e.g. via your VPN's DNS
    listenAddresses = [ "10.0.0.5" ];
    allowedCIDRs = [ "10.0.0.0/24" ];
    firewall.interfaces = [ "wg0" ];

    tls.acmeHost = "term.example.com";
    secrets = {
      sessionKeyFile = "/run/secrets/lariat-session-key";
      enrollTokenFile = "/run/secrets/lariat-enroll-token";
      credentialsFile = "/run/secrets/lariat-credentials";
    };
  };
}
