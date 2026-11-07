{
  description = "lariat: a passkey-gated PWA terminal and session dashboard for your own machine";

  # Consumers should override this with `inputs.nixpkgs.follows = "nixpkgs"`.
  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      # mkWeb pkgs { theme ? <palette attrs>; fonts ? { family; regular; bold; }; iconFont; appName; shortName; }
      lib.mkWeb = pkgs: args: pkgs.callPackage ./nix/web.nix args;

      packages = forAllSystems (
        pkgs:
        let
          own = self.packages.${pkgs.stdenv.hostPlatform.system};
        in
        {
          gateway = pkgs.callPackage ./nix/gateway.nix { };
          web = self.lib.mkWeb pkgs { };
          # `nix run github:marshallsfolly/lariat`: any Linux machine on a tailnet.
          lariat = pkgs.callPackage ./nix/lariat.nix { inherit (own) gateway web; };
          # The same with whisper.cpp (CPU) and the base.en model for the mic button.
          lariat-dictation = own.lariat.override {
            dictationServer = pkgs.whisper-cpp;
            dictationModel = pkgs.fetchurl {
              name = "ggml-base.en-q8_0.bin";
              url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en-q8_0.bin";
              hash = "sha256-pNSgdoB14Tz9fhnfOuLbxKaNN9Nqfa1F6EEMmjT4yH4=";
            };
          };
          default = own.lariat;
        }
      );

      nixosModules.lariat = import ./nix/module.nix self;
      nixosModules.default = self.nixosModules.lariat;
      homeManagerModules.lariat = import ./nix/hm-module.nix self;
      homeManagerModules.default = self.homeManagerModules.lariat;

      # `nix develop`: Python with the gateway's dependencies, for running the
      # tests and a local gateway (see README "Development").
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [
              ps.aiohttp
              ps.webauthn
              ps.cryptography
              ps.segno
            ]))
            pkgs.woff2
          ];
        };
      });
    };
}
