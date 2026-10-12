"""Rebuild Wren's logo kit with Python's standard library only.

The large mark is a geometric bird with a chevron cut out of its wing.
The small mark is drawn separately on a 16-pixel grid, not downsampled:
its eye, raised tail and chevron need to survive in a terminal. PNGs and
terminal text share that grid so their silhouettes cannot drift apart.

Run this file directly to regenerate, or pass --check to verify the kit.
This is a design tool, not part of the CLI or its runtime dependencies.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import struct
import zlib
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
COPPER = "#B86B3E"
AMBER = "#E6B17C"
INK = "#252A2E"
PAPER = "#F3EEE5"

# Transparent pixels are dots; solid pixels are hashes. The eye and wing
# are holes, not a second colour, so the mark also works in monochrome.
PIXELS = (
    "................",
    "..##............",
    "..###.....###...",
    "...##....#####..",
    "...###...##.##..",
    "....##..#######.",
    "....###.######..",
    "...##########...",
    "...###..#####...",
    "..######..###...",
    "..####..####....",
    "...########.....",
    "....######......",
    "......##.#......",
    ".....###.###....",
    "................",
)

# One compound path: outline, eye, and >-shaped wing. Even-odd fill makes
# both details transparent, including on coloured backgrounds.
BIRD = (
    "M7 7 L18 12 L28 29 C31 27 34 24 35 21 "
    "C35 14 39 9 46 9 C52 9 56 13 56 19 L62 23 L55 27 "
    "C54 39 45 49 34 52 L34 57 H40 V60 H30 V53 H26 V57 "
    "H20 V54 H23 V52 C14 50 9 45 9 38 C9 35 10 32 12 30 Z "
    "M48 18 A2 2 0 1 0 44 18 A2 2 0 1 0 48 18 Z "
    "M26 32 H30 L38 39 L30 46 H26 L33 39 Z"
)

# Custom lowercase lettering, drawn as paths rather than an installed
# font. These rounded strokes stay editable and render the same on GitHub.
LETTERS = (
    "M4 0 L14 36 L24 13 L34 36 L44 0",  # w
    "M62 36 V0 M62 14 C62 3 70 -1 80 0",  # r
    "M92 18 H124 C124 6 118 0 108 0 C97 0 90 7 90 18 "
    "C90 30 97 36 108 36 C115 36 120 33 124 29",  # e
    "M142 36 V0 M142 14 C142 -5 176 -5 176 14 V36",  # n
)


def svg(body: str, width: int, height: int, title: str, description: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">\n'
        f'  <title id="title">{escape(title)}</title>\n'
        f'  <desc id="desc">{escape(description)}</desc>\n'
        f"{body}\n</svg>\n"
    )


def bird(colour: str) -> str:
    return f'<path fill="{colour}" fill-rule="evenodd" d="{BIRD}"/>'


def wordmark(ink: str, accent: str) -> str:
    letters = "\n".join(f'    <path d="{letter}"/>' for letter in LETTERS)
    return (
        f'  <g transform="translate(16 24) scale(1.6)">{bird(accent)}</g>\n'
        f'  <g transform="translate(158 56) scale(1.4)" fill="none" '
        f'stroke="{ink}" stroke-width="7" stroke-linecap="round" stroke-linejoin="round">\n'
        f"{letters}\n  </g>"
    )


def pixel_rects(colour: str) -> str:
    cells = "\n".join(
        f'    <rect x="{x}" y="{y}" width="1" height="1"/>'
        for y, row in enumerate(PIXELS)
        for x, cell in enumerate(row)
        if cell == "#"
    )
    return f'  <g fill="{colour}" shape-rendering="crispEdges">\n{cells}\n  </g>'


def terminal() -> str:
    # A terminal cell is roughly twice as tall as it is wide. Two square
    # pixels per cell keep the bird's proportions: top/bottom/both/neither.
    blocks = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}
    return "\n".join(
        "".join(blocks[top == "#", bottom == "#"] for top, bottom in zip(PIXELS[y], PIXELS[y + 1])).rstrip()
        for y in range(0, 16, 2)
    ) + "\n"


def ascii_mark() -> str:
    # Two columns per pixel compensate for cell proportions without Unicode.
    # Omit the empty top/bottom border rows, and never emit trailing spaces.
    rows = ("".join("##" if cell == "#" else "  " for cell in row).rstrip() for row in PIXELS)
    return "\n".join(rows).strip("\n") + "\n"


def png(size: int) -> bytes:
    """Encode a transparent RGBA PNG at an integer scale, with no smoothing."""
    if size < 16 or size % 16:
        raise ValueError("pixel PNG sizes must be positive multiples of 16")
    colour = bytes.fromhex(COPPER[1:]) + b"\xff"
    scale = size // 16
    scanlines = b"".join(
        b"\x00" + b"".join(colour if PIXELS[y // scale][x // scale] == "#" else b"\x00" * 4 for x in range(size))
        for y in range(size)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines, level=9))
        + chunk(b"IEND", b"")
    )


def preview() -> str:
    """A self-contained contact sheet: real-size icons on light and dark."""
    parts = ['  <rect width="960" height="520" fill="#FCFAF6"/>',
             '  <rect x="480" width="480" height="520" fill="#1D2226"/>']
    for x, ink, accent, label in ((0, INK, COPPER, "LIGHT"), (480, PAPER, AMBER, "DARK")):
        parts.append(f'  <g transform="translate({x} 32)">{wordmark(ink, accent)}</g>')
        parts.append(f'  <g fill="{ink}" font-family="monospace" font-size="12">')
        for y, text in ((28, label), (218, "SMOOTH MARK / PIXEL MARK: 16 / 32 / 64"),
                        (348, "TERMINAL: 16 COLUMNS x 8 ROWS")):
            parts.append(f'    <text x="{x + 32}" y="{y}">{text}</text>')
        parts.append("  </g>")
        parts.append(f'  <g transform="translate({x + 32} 242)">{bird(accent)}</g>')
        for offset, size in ((144, 16), (224, 32), (320, 64)):
            parts.append(f'  <g transform="translate({x + offset} 242) scale({size / 16:g})">{pixel_rects(accent)}</g>')
        # The terminal preview uses square cells rather than depending on a
        # browser's block-glyph font. It shows the exact same 16x16 grid.
        parts.append(f'  <g transform="translate({x + 32} 370) scale(7)">{pixel_rects(accent)}</g>')
        parts.append(f'  <text x="{x + 168}" y="420" fill="{ink}" font-family="monospace" font-size="22">wren</text>')
        parts.append(f'  <text x="{x + 168}" y="446" fill="{ink}" font-family="monospace" font-size="12">small. readable. capable.</text>')
    return svg("\n".join(parts), 960, 520, "Wren logo kit preview",
               "Light and dark wordmarks, smooth and pixel icons at actual sizes, and a terminal-scale mockup.")


def artifacts() -> dict[str, bytes]:
    description = "A right-facing wren with an upright tail and a code-chevron cut out of its wing."
    text = {
        "wren-logo-light.svg": svg(wordmark(INK, COPPER), 480, 160, "Wren", description),
        "wren-logo-dark.svg": svg(wordmark(PAPER, AMBER), 480, 160, "Wren", description),
        "wren-mark.svg": svg("  " + bird(COPPER), 64, 64, "Wren mark", description),
        "wren-pixel.svg": svg(pixel_rects(COPPER), 16, 16, "Wren pixel mark", description),
        "wren-terminal.txt": terminal(),
        "wren-ascii.txt": ascii_mark(),
        "preview.svg": preview(),
    }
    result = {name: content.encode("utf-8") for name, content in text.items()}
    result.update({f"wren-pixel-{size}.png": png(size) for size in (16, 32, 128)})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report stale assets without writing anything")
    args = parser.parse_args()
    outputs = {HERE / name: content for name, content in artifacts().items()}
    # Installed apps cannot reach the repo's docs. Bundle the same mark in
    # the Python package, and keep it in sync through this generator.
    outputs[HERE.parent.parent / "src" / "wren" / "wren-terminal.txt"] = outputs[HERE / "wren-terminal.txt"]
    stale = []
    for path, content in outputs.items():
        if args.check:
            if not path.exists() or path.read_bytes() != content:
                stale.append(str(path.relative_to(HERE.parent.parent)))
        else:
            path.write_bytes(content)
    if stale:
        print("Logo assets need regenerating: " + ", ".join(stale))
        return 1
    print("Logo assets are current." if args.check else "Generated the Wren logo kit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
