# HTTPS and certificates

Passkeys (WebAuthn) only work on an HTTPS page whose certificate the phone
trusts, for a real domain name (not an IP). lariat is served on your
machine's Tailscale name, `myhost.your-tailnet.ts.net`, in one of two ways.

| | Tailscale's certificate | Your own CA |
|---|---|---|
| Setup | one switch in the admin console | nothing (lariat makes it), install the CA on the phone once |
| Phone | nothing to install | trust one CA (it can only vouch for this machine's name) |
| Privacy | the name is published in Certificate Transparency logs | nothing is published |
| Renewal | automatic | every ~2 years, `lariat tls init --renew` |

Either way lariat stays reachable only on your tailnet.

## Tailscale's certificate

1. On the [DNS page](https://login.tailscale.com/admin/dns), turn on **HTTPS
   certificates**.
2. Run `lariat`. It runs `tailscale serve --https=443 http://127.0.0.1:8795`,
   and Tailscale fetches and renews a Let's Encrypt certificate for the name.

Public certificates are logged in
[Certificate Transparency](https://certificate.transparency.dev/) logs, so
anyone can learn that `myhost.your-tailnet.ts.net` exists (not reach it).
If that bothers you, use your own CA.

## Your own CA

With Tailscale's HTTPS certificates off, lariat makes its own CA the first
time it starts (`lariat setup`, `lariat service install` or `lariat
serve`). Then put it on the phone:

```sh
lariat tls phone    # serves the CA on your tailnet; prints a QR code for the phone
```

If you turn Tailscale's certificates on later, lariat switches to them when
it next starts. To use your own CA even with them on, make it yourself:
`lariat tls init` (lariat then keeps it).

What it makes, in `~/.local/state/lariat/tls/`:

- `ca.pem`, `ca.key`: a P-256 certificate authority with an X.509 **name
  constraint**: it is only valid for this one name. Even if its key leaked, it
  could not sign a certificate your phone would accept for any other site.
- `cert.pem`, `key.pem`: the server certificate for the name, valid for 800
  days (iOS refuses more than 825).

With these present, `lariat serve` terminates TLS itself and asks Tailscale
only to forward TCP (`tailscale serve --tcp=443 tcp://127.0.0.1:8795`):
Tailscale never issues a certificate, and HTTPS certificates can stay off in
the admin console. `lariat serve --tailscale-cert` ignores your CA.

### Installing the CA on the phone

`lariat tls phone` serves a page with the CA certificate over plain HTTP, on
your Tailscale address only, until you press Ctrl-C. Scan its QR code with the
phone's camera (Tailscale on), then:

**iPhone and iPad**

1. Tap **Download the certificate**, then **Allow**. iOS says "Profile
   Downloaded".
2. Open **Settings**. Tap **Profile Downloaded** at the top (or General → VPN &
   Device Management → the profile), tap **Install**, enter your passcode, and
   **Install** again.
3. Settings → General → About → **Certificate Trust Settings**. Turn on
   **lariat CA (myhost.your-tailnet.ts.net)** and confirm. Safari will not
   trust it for websites until you do this step.

**Android**

1. Tap **Download the certificate**; it lands in Downloads.
2. Settings → Security (and privacy) → More security settings → Encryption &
   credentials → **Install a certificate** → **CA certificate** → Install
   anyway → pick the file. (Menu names vary by phone; search Settings for
   "CA certificate".) Android shows a "network may be monitored" notice for
   any user CA; that is expected.

Then open `https://myhost.your-tailnet.ts.net/` (or scan the enrollment QR
code from `lariat`): no certificate warning means it worked.

### Renewing

`lariat doctor` shows how many days the certificate has left. Before it runs
out:

```sh
lariat tls init --renew    # new server certificate from the same CA
```

and restart lariat. Nothing changes on the phone: it trusts the CA, not the
certificate.

### Keeping the CA key safe

`ca.key` is only needed to renew. It lives in lariat's state directory,
readable only by you. If you would rather not keep it on the machine, move it
somewhere offline and put it back to renew. If you lose it, delete the `tls`
directory, run `lariat tls init` again, and install the new CA on the phone.

### Removing the CA from the phone

- iPhone: Settings → General → VPN & Device Management → lariat CA → **Remove
  Profile**.
- Android: Encryption & credentials → User credentials → lariat CA → Remove.

## Your own CA on NixOS

The NixOS module's `frontend = "nginx"` serves lariat with any certificate
you give it, on your tailnet address. Make the CA and certificate once, on any
machine with Nix:

```sh
nix run github:marshallsfolly/lariat -- tls init --host myhost.your-tailnet.ts.net --state ./lariat-ca
```

- `lariat-ca/tls/cert.pem` is public: commit it to your config.
- `lariat-ca/tls/key.pem` is a secret: put it in your secrets manager. With
  sops-nix:

  ```sh
  sops --encrypt --input-type binary --output-type binary lariat-ca/tls/key.pem > secrets/lariat-tls.key
  ```

- `lariat-ca/tls/ca.key`: keep it offline; you need it every two years to
  renew.

```nix
{ config, ... }:
{
  sops.secrets."lariat-tls-key" = {
    sopsFile = ./secrets/lariat-tls.key;
    format = "binary";
    owner = config.services.nginx.user;
  };

  services.lariat = {
    enable = true;
    frontend = "nginx";
    user = "alice";
    hostName = "myhost.your-tailnet.ts.net";
    listenAddresses = [ "100.101.102.103" ];  # `tailscale ip -4`
    nonlocalBind = true;                      # nginx may start before Tailscale has the address
    nginx.after = [ "tailscaled.service" ];
    allowedCIDRs = [ "100.64.0.0/10" ];
    firewall.interfaces = [ config.services.tailscale.interfaceName ];
    tls.certFile = ./lariat-ca/tls/cert.pem;
    tls.keyFile = config.sops.secrets."lariat-tls-key".path;
  };
}
```

Install the CA on the phone from the same directory (any machine on the
tailnet works; the page is only a download):

```sh
nix run github:marshallsfolly/lariat -- tls phone --host myhost.your-tailnet.ts.net --state ./lariat-ca
```

To renew: `nix run github:marshallsfolly/lariat -- tls init --renew --host … --state ./lariat-ca`,
re-encrypt `key.pem`, deploy.

## Already have a CA?

Any certificate works if the phone trusts its issuer and it has: the name in
subjectAltName, Extended Key Usage serverAuth, at most 825 days validity
(iOS), an RSA ≥ 2048 or ECDSA P-256/P-384 key. On NixOS use the nginx
frontend as above; elsewhere, run lariat behind your own reverse proxy with
`lariat serve --no-tailscale-serve` and proxy HTTPS (WebSockets included) to
`127.0.0.1:8795`.
