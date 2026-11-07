# The gateway: aiohttp + py_webauthn. The unit tests (pure, no network) run as
# part of the build. Tool paths (tmux, herdr, ffmpeg, whisper) come from the
# environment the NixOS module sets, not from this package.
{ lib, python3Packages }:
python3Packages.buildPythonApplication {
  pname = "lariat-gateway";
  version = "0.1.0";
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../gateway;
    fileset = lib.fileset.unions [
      ../gateway/pyproject.toml
      (lib.fileset.fileFilter (f: f.hasExt "py") ../gateway/lariat)
      (lib.fileset.fileFilter (f: f.hasExt "py") ../gateway/tests)
    ];
  };

  build-system = [ python3Packages.setuptools ];
  dependencies = with python3Packages; [
    aiohttp
    cryptography
    segno
    webauthn
  ];

  nativeCheckInputs = [ python3Packages.unittestCheckHook ];
  unittestFlags = [
    "-s"
    "tests"
    "-t"
    "."
    "-v"
  ];
  pythonImportsCheck = [ "lariat" ];

  meta = {
    description = "Gateway for lariat, a passkey-gated PWA terminal and session dashboard";
    license = lib.licenses.mit;
    mainProgram = "lariat-gateway";
    platforms = lib.platforms.unix;
  };
}
