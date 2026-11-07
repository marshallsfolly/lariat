# `lariat`: the command for running lariat without the NixOS module
# (`nix run`, home-manager; Linux or macOS with Nix, on a tailnet). It wraps the
# gateway with the web bundle; give it whisper-cpp and a model for dictation.
{
  lib,
  runCommand,
  makeWrapper,
  gateway,
  web,
  dictationServer ? null, # a whisper-cpp package (provides whisper-server)
  dictationModel ? null, # a ggml model file
}:
let
  dictation = dictationServer != null && dictationModel != null;
in
runCommand "lariat"
  {
    nativeBuildInputs = [ makeWrapper ];
    passthru = { inherit gateway web; };
    meta = gateway.meta // {
      description = "Passkey-gated PWA terminal and agent dashboard for your tailnet";
      mainProgram = "lariat";
    };
  }
  ''
    makeWrapper ${lib.getExe' gateway "lariat"} $out/bin/lariat \
      --set-default LARIAT_STATIC ${web} \
      ${lib.optionalString dictation ''
        --set-default LARIAT_DICTATE_SERVER ${lib.getExe' dictationServer "whisper-server"} \
        --set-default LARIAT_DICTATE_MODEL ${dictationModel}
      ''}
  ''
