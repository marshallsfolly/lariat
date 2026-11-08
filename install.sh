#!/bin/sh
# Install lariat (Linux, macOS): the release binary for this machine, checked
# against the release's SHA256SUMS, into ~/.local/bin; then `lariat setup`.
#
#   curl -fsSL https://raw.githubusercontent.com/marshallsfolly/lariat/main/install.sh | sh
#
# Environment: LARIAT_VERSION (a tag like v0.1.0; default: the latest release),
# LARIAT_BIN_DIR (default: ~/.local/bin), LARIAT_NO_SETUP=1 (install only).
set -eu

repo="${LARIAT_REPO:-marshallsfolly/lariat}"
dir="${LARIAT_BIN_DIR:-$HOME/.local/bin}"

say() { printf '%s\n' "$*"; }
die() { printf 'lariat: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=macos ;;
  *) die "no release binary for $(uname -s); see https://github.com/$repo#get-started" ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=x86_64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) die "no release binary for $(uname -m); see https://github.com/$repo#get-started" ;;
esac
if [ -e /etc/NIXOS ]; then
  die "on NixOS, use the module or 'nix run github:$repo' (docs/nixos.md); release binaries need a regular Linux"
fi
command -v curl > /dev/null || die "needs curl"

asset="lariat-$os-$arch"
if [ -n "${LARIAT_VERSION:-}" ]; then
  base="https://github.com/$repo/releases/download/$LARIAT_VERSION"
else
  base="https://github.com/$repo/releases/latest/download"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM
say "Downloading $asset..."
curl -fsSL "$base/$asset" -o "$tmp/$asset" || die "download failed: $base/$asset"
curl -fsSL "$base/SHA256SUMS" -o "$tmp/SHA256SUMS" || die "download failed: $base/SHA256SUMS"

want="$(awk -v f="$asset" '$2 == f || $2 == "*" f { print $1 }' "$tmp/SHA256SUMS")"
if command -v sha256sum > /dev/null; then
  got="$(sha256sum "$tmp/$asset" | cut -d' ' -f1)"
else
  got="$(shasum -a 256 "$tmp/$asset" | cut -d' ' -f1)"
fi
if [ -z "$want" ] || [ "$want" != "$got" ]; then
  die "the download does not match the release's SHA256SUMS; not installed"
fi

mkdir -p "$dir"
chmod 755 "$tmp/$asset"
mv -f "$tmp/$asset" "$dir/lariat"
say "Installed $("$dir/lariat" --version) to $dir/lariat"

case ":$PATH:" in
  *":$dir:"*) ;;
  *)
    say ""
    say "$dir is not on your PATH yet. Add it (then open a new terminal):"
    case "${SHELL:-}" in
      */zsh) say "    echo 'export PATH=\"$dir:\$PATH\"' >> ~/.zshrc" ;;
      */fish) say "    fish_add_path $dir" ;;
      *) say "    echo 'export PATH=\"$dir:\$PATH\"' >> ~/.bashrc" ;;
    esac
    ;;
esac

if [ -n "${LARIAT_NO_SETUP:-}" ]; then
  say "Next: lariat setup"
elif [ -r /dev/tty ] && { : < /dev/tty; } 2> /dev/null; then
  say ""
  # Piped into sh, our stdin is this script: the questions need the terminal.
  exec "$dir/lariat" setup < /dev/tty
else
  say "Next: $dir/lariat setup"
fi
