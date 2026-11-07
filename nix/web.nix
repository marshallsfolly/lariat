# The static PWA bundle: web/ + theme + fonts (as WOFF2) + pinned xterm.js,
# assembled by web/build.py. Exposed as `lib.mkWeb pkgs { ... }`.
{
  lib,
  callPackage,
  runCommand,
  python3,
  woff2,
  nerd-fonts,
  # A palette attrset (nix/themes/schema.md). Non-string values are ignored.
  theme ? import ./themes/default.nix,
  # { family; regular; bold ? null; } with TTF/OTF paths; null = JetBrainsMono Nerd Font Mono.
  fonts ? null,
  # Full-size Nerd Font icons for the UI (subset at build time to the ones used);
  # null falls back to the text font's (cell-sized) icons.
  iconFont ? "${nerd-fonts.symbols-only}/share/fonts/truetype/NerdFonts/Symbols/SymbolsNerdFont-Regular.ttf",
  appName ? "lariat",
  shortName ? appName,
  vendor ? callPackage ./vendor.nix { },
}:
let
  jetbrains = "${nerd-fonts.jetbrains-mono}/share/fonts/truetype/NerdFonts/JetBrainsMono";
  font =
    if fonts != null then
      { bold = null; } // fonts
    else
      {
        family = "JetBrainsMono Nerd Font Mono";
        regular = "${jetbrains}/JetBrainsMonoNerdFontMono-Regular.ttf";
        bold = "${jetbrains}/JetBrainsMonoNerdFontMono-Bold.ttf";
      };
  palette = lib.filterAttrs (n: v: builtins.isString v || n == "ansi16") theme;
  themeJson = builtins.toFile "lariat-theme.json" (builtins.toJSON palette);
  src = lib.fileset.toSource {
    root = ../web;
    fileset = lib.fileset.unions [
      ../web/index.html
      ../web/app.css
      ../web/sw.js
      ../web/build.py
      ../web/js
    ];
  };
  ext = path: lib.last (lib.splitString "." (baseNameOf (toString path)));
in
runCommand "lariat-web"
  {
    nativeBuildInputs = [
      (python3.withPackages (ps: [
        ps.fonttools
        ps.brotli
      ]))
      woff2
    ];
    passthru = { inherit vendor font; };
  }
  ''
    cp ${font.regular} regular.${ext font.regular}
    chmod u+w regular.*
    woff2_compress regular.${ext font.regular}
    ${lib.optionalString (font.bold != null) ''
      cp ${font.bold} bold.${ext font.bold}
      chmod u+w bold.*
      woff2_compress bold.${ext font.bold}
    ''}
    python3 ${src}/build.py \
      --src ${src} \
      --theme ${themeJson} \
      --vendor ${vendor} \
      --font-regular regular.woff2 \
      ${lib.optionalString (font.bold != null) "--font-bold bold.woff2"} \
      ${lib.optionalString (iconFont != null) "--icons ${iconFont}"} \
      --app-name ${lib.escapeShellArg appName} \
      --short-name ${lib.escapeShellArg shortName} \
      --out $out
  ''
