# alle — brand assets

## The mark

The icon is **∀** — the "for all" universal-quantifier symbol — which is also a
stylized **A**. It carries the brand on two levels:

- **alle = "all"** (German). ∀ literally reads "for all" → the *universal* VPN client.
- Its **three tips are exit nodes**: one client fanning out to many VPN exits/locations
  ("all your providers, all your locations, one place").

It's deliberately minimal so it stays legible from a 1024px app icon down to a
menu-bar glyph. The wordmark keeps the ∀ mark as the anchor and sets **alle** as a
soft, tightly-spaced lowercase lockup for README and web UI headers.

## Files

| File                          | Use                                                                                                                       |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `icon.svg`                    | Full-colour app icon (indigo→cyan squircle, white mark). Source of truth for the primary app icon.                        |
| `icon-mono.svg`               | Single-colour **template** mark for menu-bar/tray. Uses `currentColor` so the OS tints it for light/dark; black fallback. |
| `wordmark.svg`                | Primary ∀ + `alle` lockup for README/web UI headers.                                                                      |
| `icon-violet-magenta.svg`     | Alternate warmer colourway for comparison.                                                                                |
| `wordmark-violet-magenta.svg` | Wordmark using the violet→magenta colourway.                                                                              |
| `icon-premium-dark.svg`       | Alternate darker, premium colourway for comparison.                                                                       |
| `wordmark-premium-dark.svg`   | Wordmark using the premium dark colourway.                                                                                |
| `png/icon-*.png`              | Exported primary app-icon PNGs at 16, 32, 64, 128, 256, 512, and 1024px.                                                  |
| `png/wordmark-*.png`          | Exported primary wordmark PNGs at 320, 640, and 1280px wide.                                                             |
| `icon.icns`                   | macOS app icon bundle.                                                                                                    |
| `icon.ico`                    | Windows/icon-browser bundle.                                                                                              |
| `icon.iconset/`               | Intermediate macOS iconset used to build `icon.icns`.                                                                     |

## Colours

Primary:

- Gradient: `#4f46e5` (indigo) → `#06b6d4` (cyan), diagonal.
- Wordmark text: deep ink `#171e4f` through indigo to cyan.
- Mark: `#ffffff`.

Alternate comparison directions:

- **Violet→magenta:** `#6d28d9` → `#a21caf` → `#ec4899`; warmer and more consumer-app.
- **Premium dark:** `#273270` → `#10172f` → `#030712` with cyan/indigo highlights;
  quieter and more infrastructure/tooling.

## Exporting

SVGs are the source. Rasterise with `rsvg-convert`:

```bash
# app icon PNGs
mkdir -p assets/png
for s in 16 32 64 128 256 512 1024; do
  rsvg-convert -w $s -h $s assets/icon.svg -o assets/png/icon-$s.png
done

# wordmark PNGs
rsvg-convert -w 320 -h 86 assets/wordmark.svg -o assets/png/wordmark-320.png
rsvg-convert -w 640 -h 172 assets/wordmark.svg -o assets/png/wordmark-640.png
rsvg-convert -w 1280 -h 344 assets/wordmark.svg -o assets/png/wordmark-1280.png

# macOS .icns
mkdir -p assets/icon.iconset
rsvg-convert -w 16 -h 16 assets/icon.svg -o assets/icon.iconset/icon_16x16.png
rsvg-convert -w 32 -h 32 assets/icon.svg -o assets/icon.iconset/icon_16x16@2x.png
rsvg-convert -w 32 -h 32 assets/icon.svg -o assets/icon.iconset/icon_32x32.png
rsvg-convert -w 64 -h 64 assets/icon.svg -o assets/icon.iconset/icon_32x32@2x.png
rsvg-convert -w 128 -h 128 assets/icon.svg -o assets/icon.iconset/icon_128x128.png
rsvg-convert -w 256 -h 256 assets/icon.svg -o assets/icon.iconset/icon_128x128@2x.png
rsvg-convert -w 256 -h 256 assets/icon.svg -o assets/icon.iconset/icon_256x256.png
rsvg-convert -w 512 -h 512 assets/icon.svg -o assets/icon.iconset/icon_256x256@2x.png
rsvg-convert -w 512 -h 512 assets/icon.svg -o assets/icon.iconset/icon_512x512.png
rsvg-convert -w 1024 -h 1024 assets/icon.svg -o assets/icon.iconset/icon_512x512@2x.png
iconutil -c icns assets/icon.iconset -o assets/icon.icns

# Windows .ico
magick assets/png/icon-16.png assets/png/icon-32.png assets/png/icon-64.png \
  assets/png/icon-128.png assets/png/icon-256.png assets/icon.ico
```

The menu-bar variant should be used as a template image (let the OS colour it),
not rendered with a fixed colour.
