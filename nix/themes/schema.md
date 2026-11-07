# Theme schema

`services.lariat.theme.palette` is an attrset of colours. Values are 6-digit
hex strings, with or without a leading `#`. Keys that are not listed here, and
values that are not strings (functions, nested attrsets), are ignored, so a
desktop palette file with extra helpers can be passed in as is.

## Required keys

| Key | Used for |
|---|---|
| `crust` | darkest background (inputs, pills) |
| `mantle` | app chrome: header, key bar, sheets; also the PWA `theme_color` |
| `base` | terminal background; PWA `background_color` |
| `surface0` `surface1` `surface2` | cards, borders, pressed states, terminal selection (`surface2`) |
| `overlay0` `overlay1` | scrollbar thumb, dim glyphs |
| `text` | body text, terminal foreground |
| `bright` | headings, selected text |
| `subtext` `muted` `faint` | secondary text, from more to less prominent |
| `red` `green` `yellow` `blue` `purple` `aqua` `orange` | status colours and bright ANSI |
| `redDim` `greenDim` `yellowDim` `blueDim` `purpleDim` `aquaDim` | normal ANSI 1-6 |
| `accent` | primary accent: buttons, cursor, the icon's chevron |
| `accent2` | secondary accent: agent glyphs |

## Optional keys

| Key | Default |
|---|---|
| `orangeDim` | `orange` |
| `ansi16` | a list of 16 hex strings for the terminal's ANSI palette. Default order: `base redDim greenDim yellowDim blueDim purpleDim aquaDim muted faint red green yellow blue purple aqua text` |

## Status colours

working = `yellow` (pulsing), blocked = `red`, done = `green`, idle = `muted`,
unknown = `faint`.

## Fonts

`services.lariat.theme.fonts = { family; regular; bold; }`. `regular` and
`bold` are paths to TTF/OTF files; they are converted to WOFF2 at build time
and served from the gateway as "lariat-mono". Use a **Nerd Font Mono**
build: the UI draws its icons from Nerd Font glyphs. `family` is informational.
