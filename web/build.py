#!/usr/bin/env python3
"""Assemble the static PWA bundle.

Used by nix/web.nix at build time and by hand for local development:

    python3 web/build.py --src web --theme theme.json --vendor <dir> \
        --font-regular R.woff2 --font-bold B.woff2 [--icons SymbolsNerdFont.ttf] \
        --out /tmp/lariat-web

It templates the app name and colours into index.html and the manifest,
writes palette.css (colour tokens + @font-face), js/build-info.js (xterm
theme), renders the icons (SVG and PNG, no image tools needed) and writes a
service worker whose cache name is a hash of the whole bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import struct
import sys
import zlib
from pathlib import Path

PALETTE_KEYS = [
    "base", "mantle", "crust", "surface0", "surface1", "surface2", "overlay0", "overlay1",
    "text", "bright", "subtext", "muted", "faint",
    "red", "green", "yellow", "blue", "purple", "aqua", "orange",
    "redDim", "greenDim", "yellowDim", "blueDim", "purpleDim", "aquaDim",
    "accent", "accent2",
]  # fmt: skip
ANSI_DEFAULT = [
    "base", "redDim", "greenDim", "yellowDim", "blueDim", "purpleDim", "aquaDim", "muted",
    "faint", "red", "green", "yellow", "blue", "purple", "aqua", "text",
]  # fmt: skip
ANSI_NAMES = [
    "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "brightBlack", "brightRed", "brightGreen", "brightYellow", "brightBlue", "brightMagenta", "brightCyan", "brightWhite",
]  # fmt: skip


def hexcolor(value: str, key: str) -> str:
    v = str(value).strip().lstrip("#").lower()
    if len(v) != 6 or any(c not in "0123456789abcdef" for c in v):
        raise SystemExit(f"theme: {key} must be a 6-digit hex colour, got {value!r}")
    return v


def load_theme(path: Path) -> dict:
    raw = json.loads(path.read_text())
    missing = [k for k in PALETTE_KEYS if k not in raw]
    if missing:
        raise SystemExit(f"theme: missing keys {', '.join(missing)} (see nix/themes/schema.md)")
    theme = {k: hexcolor(raw[k], k) for k in PALETTE_KEYS}
    theme["orangeDim"] = hexcolor(raw.get("orangeDim", raw["orange"]), "orangeDim")
    ansi = raw.get("ansi16")
    if ansi is not None:
        if not isinstance(ansi, list) or len(ansi) != 16:
            raise SystemExit("theme: ansi16 must be a list of 16 hex colours")
        theme["ansi16"] = [hexcolor(c, f"ansi16[{i}]") for i, c in enumerate(ansi)]
    else:
        theme["ansi16"] = [theme[k] for k in ANSI_DEFAULT]
    return theme


def rgb(h: str) -> tuple[int, int, int]:
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# -- icons: signed-distance rendering of ">_" ---------------------------------

def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _rrect_dist(px, py, size, radius):
    h = size / 2
    qx, qy = abs(px - h) - (h - radius), abs(py - h) - (h - radius)
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - radius


ICON_GLYPH = {
    # On a 512 grid: the chevron and the cursor bar, round-capped strokes.
    "chevron": [(148, 176), (258, 256), (148, 336)],
    "bar": [(286, 336), (374, 336)],
    "width": 46,
}


def render_icon(size: int, theme: dict, *, rounded: bool, scale: float = 1.0) -> bytes:
    bg = rgb(theme["mantle"])
    chev = rgb(theme["accent"])
    bar = rgb(theme["text"])
    k = size / 512
    half = ICON_GLYPH["width"] / 2 * k * scale

    def pt(p):
        return ((p[0] - 256) * scale + 256) * k, ((p[1] - 256) * scale + 256) * k

    c = [pt(p) for p in ICON_GLYPH["chevron"]]
    b = [pt(p) for p in ICON_GLYPH["bar"]]
    radius = size * 0.225
    rows = []
    for y in range(size):
        row = bytearray([0])  # filter: none
        py = y + 0.5
        for x in range(size):
            px = x + 0.5
            a = 1.0
            if rounded:
                a = max(0.0, min(1.0, 0.5 - _rrect_dist(px, py, size, radius)))
            if a == 0.0:
                row += b"\0\0\0\0"
                continue
            d1 = min(_seg_dist(px, py, *c[0], *c[1]), _seg_dist(px, py, *c[1], *c[2])) - half
            d2 = _seg_dist(px, py, *b[0], *b[1]) - half
            col = bg
            for d, fg in ((d1, chev), (d2, bar)):
                cov = max(0.0, min(1.0, 0.5 - d))
                if cov:
                    col = tuple(round(fg[i] * cov + col[i] * (1 - cov)) for i in range(3))
            row += bytes([col[0], col[1], col[2], round(a * 255)])
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", raw)
        + chunk(b"IEND", b"")
    )


def icon_svg(theme: dict) -> str:
    c = " ".join(f"{x},{y}" for x, y in ICON_GLYPH["chevron"])
    (x1, y1), (x2, y2) = ICON_GLYPH["bar"]
    w = ICON_GLYPH["width"]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
        f'<rect width="512" height="512" rx="115" fill="#{theme["mantle"]}"/>'
        f'<polyline points="{c}" fill="none" stroke="#{theme["accent"]}" stroke-width="{w}" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#{theme["text"]}" stroke-width="{w}" '
        'stroke-linecap="round"/></svg>\n'
    )


# -- bundle -------------------------------------------------------------------

# Private Use Area code points, as characters or CSS escapes ("\\f0f3").
PUA = re.compile(r"[\ue000-\uf8ff\U000f0000-\U000ffffd]")
CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{4,6})")


def used_icons(src: Path) -> set[int]:
    """Every icon code point the app's sources mention."""
    points: set[int] = set()
    for path in [src / "index.html", src / "app.css", *sorted((src / "js").glob("*.js"))]:
        text = path.read_text()
        points.update(ord(ch) for ch in PUA.findall(text))
        for hexa in CSS_ESCAPE.findall(text):
            cp = int(hexa, 16)
            if 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0xFFFFD:
                points.add(cp)
    return points


def subset_icons(font: Path, points: set[int], dest: Path) -> int:
    """Cut the icon font down to `points`, as WOFF2. Returns the glyph count."""
    from fontTools import subset  # build-time only

    options = subset.Options()
    options.flavor = "woff2"
    options.layout_features = []
    options.name_IDs = ["*"]  # keep the font's licence and copyright records
    options.notdef_outline = True
    options.drop_tables += ["PfEd"]  # FontForge metadata; fontTools cannot subset it
    sub = subset.Subsetter(options)
    tt = subset.load_font(str(font), options)
    sub.populate(unicodes=points)
    sub.subset(tt)
    subset.save_font(tt, str(dest), options)
    return len(tt.getGlyphOrder())


def palette_css(theme: dict, fonts: dict[str, str]) -> str:
    lines = ["/* generated by web/build.py from the theme; do not edit */"]
    if "icons" in fonts:
        lines.append(
            f'@font-face {{ font-family: "lariat-icons"; src: url("/fonts/{fonts["icons"]}") format("woff2"); '
            "font-display: block; }"
        )
    for weight, name in (("400", "regular"), ("700", "bold")):
        if name in fonts:
            fmt = "woff2" if fonts[name].endswith(".woff2") else "truetype"
            lines.append(
                f'@font-face {{ font-family: "lariat-mono"; src: url("/fonts/{fonts[name]}") format("{fmt}"); '
                f"font-weight: {weight}; font-style: normal; font-display: block; }}"
            )
    lines.append(":root {")
    for key in PALETTE_KEYS + ["orangeDim"]:
        lines.append(f"  --{key}: #{theme[key]};")
    for key in ("crust", "mantle", "base", "accent", "red", "green", "yellow"):
        r, g, b = rgb(theme[key])
        lines.append(f"  --{key}-rgb: {r} {g} {b};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def xterm_theme(theme: dict) -> dict:
    out = {
        "background": "#" + theme["base"],
        "foreground": "#" + theme["text"],
        "cursor": "#" + theme["accent"],
        "cursorAccent": "#" + theme["base"],
        "selectionBackground": "#" + theme["surface2"],
        "selectionForeground": "#" + theme["bright"],
        "scrollbarSliderBackground": "#" + theme["surface1"] + "99",
        "scrollbarSliderHoverBackground": "#" + theme["surface2"] + "cc",
        "scrollbarSliderActiveBackground": "#" + theme["overlay0"] + "cc",
    }
    for name, color in zip(ANSI_NAMES, theme["ansi16"]):
        out[name] = "#" + color
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--theme", type=Path, required=True)
    ap.add_argument("--vendor", type=Path, required=True, help="dir with xterm.mjs, xterm.css and addon-*.mjs")
    ap.add_argument("--font-regular", type=Path, required=True)
    ap.add_argument("--font-bold", type=Path)
    ap.add_argument("--icons", type=Path, help="a full-size Nerd Font (e.g. Symbols Nerd Font) for UI icons; subset to what the app uses")
    ap.add_argument("--app-name", default="lariat")
    ap.add_argument("--short-name")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    theme = load_theme(args.theme)
    out: Path = args.out
    if out.exists():
        shutil.rmtree(out)
    (out / "js").mkdir(parents=True)
    (out / "vendor").mkdir()
    (out / "fonts").mkdir()
    (out / "icons").mkdir()

    app_name = args.app_name
    short_name = args.short_name or app_name
    subst = {
        "@APP_NAME@": _html(app_name),
        "@SHORT_NAME@": _html(short_name),
        "@THEME_COLOR@": "#" + theme["mantle"],
    }

    regular_name = f"mono-regular{args.font_regular.suffix}"
    subst["@FONT_REGULAR@"] = regular_name
    subst["@FONT_TYPE@"] = "font/woff2" if regular_name.endswith(".woff2") else "font/ttf"
    index = (args.src / "index.html").read_text()
    for k, v in subst.items():
        index = index.replace(k, v)
    (out / "index.html").write_text(index)
    shutil.copy(args.src / "app.css", out / "app.css")
    for js in sorted((args.src / "js").glob("*.js")):
        shutil.copy(js, out / "js" / js.name)

    for name in ("xterm.mjs", "xterm.css", "addon-fit.mjs", "addon-webgl.mjs", "addon-unicode11.mjs", "addon-web-links.mjs"):
        shutil.copy(args.vendor / name, out / "vendor" / name)

    fonts = {}
    for key, src in (("regular", args.font_regular), ("bold", args.font_bold)):
        if src is None:
            continue
        name = f"mono-{key}{src.suffix}"
        shutil.copy(src, out / "fonts" / name)
        fonts[key] = name
    if args.icons:
        points = used_icons(args.src)
        count = subset_icons(args.icons, points, out / "fonts" / "icons.woff2")
        print(f"icons: {len(points)} code points, {count} glyphs", file=sys.stderr)
        fonts["icons"] = "icons.woff2"
    (out / "palette.css").write_text(palette_css(theme, fonts))

    (out / "icons" / "icon.svg").write_text(icon_svg(theme))
    (out / "icons" / "apple-touch-icon-180.png").write_bytes(render_icon(180, theme, rounded=False))
    (out / "icons" / "icon-192.png").write_bytes(render_icon(192, theme, rounded=True))
    (out / "icons" / "icon-512.png").write_bytes(render_icon(512, theme, rounded=True))
    (out / "icons" / "icon-maskable-512.png").write_bytes(render_icon(512, theme, rounded=False, scale=0.78))

    manifest = {
        "name": app_name,
        "short_name": short_name,
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#" + theme["base"],
        "theme_color": "#" + theme["mantle"],
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    (out / "manifest.webmanifest").write_text(json.dumps(manifest, indent=2) + "\n")

    build_info = {
        "theme": xterm_theme(theme),
        "colors": {k: "#" + theme[k] for k in ("base", "mantle", "accent")},
        "fontFamily": '"lariat-mono", ui-monospace, monospace',
        "fontBold": "bold" in fonts,
    }
    (out / "js" / "build-info.js").write_text(
        "// generated by web/build.py\nexport const BUILD = " + json.dumps(build_info, indent=2) + ";\n"
    )

    # Service worker last: its cache name is a hash over everything above.
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    digest = hashlib.sha256()
    for rel in files:
        digest.update(rel.encode() + b"\0" + (out / rel).read_bytes())
    version = digest.hexdigest()[:16]
    precache = ["/"] + ["/" + f for f in files if f != "index.html"]
    sw = (args.src / "sw.js").read_text()
    sw = sw.replace("@VERSION@", version).replace("@PRECACHE@", json.dumps(precache))
    (out / "sw.js").write_text(sw)
    print(f"lariat web bundle {version}: {len(files) + 1} files")


def _html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


if __name__ == "__main__":
    main()
