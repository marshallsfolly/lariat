# A NixOS host on a tailnet with herdr agents and fast dictation on the GPU.
# `tailscale serve` provides HTTPS with the node's ts.net certificate; the
# gateway keeps its own secrets. Run `lariat-enroll` as alice for the QR code.
{ inputs, pkgs, ... }:
{
  imports = [ inputs.lariat.nixosModules.default ];

  services.tailscale.enable = true;

  services.lariat = {
    enable = true;
    user = "alice";
    hostName = "myhost.your-tailnet.ts.net"; # this machine's MagicDNS name
    appName = "myhost"; # the home-screen label

    herdr = {
      # herdr is not in nixpkgs; e.g. inputs.llm-agents.url = "github:numtide/llm-agents.nix"
      package = inputs.llm-agents.packages.${pkgs.stdenv.hostPlatform.system}.herdr;
      agentKinds = [
        "claude"
        "codex"
      ];
    };
    tmux.defaultSession = "main";
    projectRoots = [ "~/code" ];

    dictation = {
      enable = true;
      package = pkgs.whisper-cpp-vulkan; # any GPU with a Vulkan driver
      model = pkgs.fetchurl {
        name = "ggml-small.en-q8_0.bin";
        url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en-q8_0.bin";
        hash = "sha256-Z6F59gjqYRS9P9uQYOditYij+zvQDEOHlxvk0XeVgGc=";
      };
      # prompt = "Claude, NixOS, tmux, pull request.";  # words to expect
    };
  };
}
