# xterm.js and its addons as ES modules, from pinned npm tarballs (fixed-output,
# hashed). The only network fetch in the build; nothing is fetched at runtime.
{ fetchurl, runCommand }:
let
  npm =
    name: version: hash:
    fetchurl {
      url = "https://registry.npmjs.org/@xterm/${name}/-/${name}-${version}.tgz";
      inherit hash;
    };
  xterm = npm "xterm" "6.0.0" "sha512-TQwDdQGtwwDt+2cgKDLn0IRaSxYu1tSUjgKarSDkUM0ZNiSRXFpjxEsvc/Zgc5kq5omJ+V0a8/kIM2WD3sMOYg==";
  addons = {
    addon-fit = npm "addon-fit" "0.11.0" "sha512-jYcgT6xtVYhnhgxh3QgYDnnNMYTcf8ElbxxFzX0IZo+vabQqSPAjC3c1wJrKB5E19VwQei89QCiZZP86DCPF7g==";
    addon-webgl = npm "addon-webgl" "0.19.0" "sha512-b3fMOsyLVuCeNJWxolACEUED0vm7qC0cy4wRvf3oURSzDTYVQiGPhTnhWZwIHdvC48Y+oLhvYXnY4XDXPoJo6A==";
    addon-unicode11 = npm "addon-unicode11" "0.9.0" "sha512-FxDnYcyuXhNl+XSqGZL/t0U9eiNb/q3EWT5rYkQT/zuig8Gz/VagnQANKHdDWFM2lTMk9ly0EFQxxxtZUoRetw==";
    addon-web-links = npm "addon-web-links" "0.12.0" "sha512-4Smom3RPyVp7ZMYOYDoC/9eGJJJqYhnPLGGqJ6wOBfB8VxPViJNSKdgRYb8NpaM6YSelEKbA2SStD7lGyqaobw==";
  };
in
runCommand "lariat-xterm-6.0.0" { } ''
  mkdir -p $out/licenses
  unpack() { rm -rf pkg && mkdir pkg && tar -xzf "$1" -C pkg --strip-components=1; }

  unpack ${xterm}
  cp pkg/lib/xterm.mjs pkg/css/xterm.css $out/
  cp pkg/LICENSE $out/licenses/xterm

  ${builtins.concatStringsSep "\n" (
    builtins.attrValues (
      builtins.mapAttrs (name: src: ''
        unpack ${src}
        cp pkg/lib/${name}.mjs $out/
        cp pkg/LICENSE $out/licenses/${name}
      '') addons
    )
  )}
''
