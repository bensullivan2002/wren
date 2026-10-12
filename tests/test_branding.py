"""The logo kit should reproduce exactly, including its tiny terminal mark.

These are asset integrity tests, not claims about aesthetic quality. They
use only the standard library and never regenerate files during a test.
"""

from pathlib import Path
import runpy
import struct
import xml.etree.ElementTree as ET
import zlib

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "branding"
SOURCE = runpy.run_path(str(ASSETS / "generate.py"))
PIXELS = SOURCE["PIXELS"]
ARTIFACTS = SOURCE["artifacts"]()
SVG = "{http://www.w3.org/2000/svg}"


def test_pixel_master_is_a_connected_16_square_silhouette():
    assert len(PIXELS) == 16
    assert all(len(row) == 16 and set(row) <= {".", "#"} for row in PIXELS)
    solid = {(x, y) for y, row in enumerate(PIXELS) for x, cell in enumerate(row) if cell == "#"}
    visited = set()
    pending = [min(solid)]
    while pending:
        x, y = pending.pop()
        if (x, y) in visited:
            continue
        visited.add((x, y))
        pending.extend(point for point in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))
                       if point in solid and point not in visited)
    assert visited == solid  # no stray, disconnected pixels
    assert PIXELS[4][11] == "."  # the eye stays a one-pixel cutout
    assert all(PIXELS[y][x] == "." for x, y in ((6, 8), (8, 9), (6, 10)))  # > wing


@pytest.mark.parametrize("name", sorted(ARTIFACTS))
def test_committed_assets_match_their_editable_source(name):
    assert (ASSETS / name).read_bytes() == ARTIFACTS[name], "Run python3 docs/branding/generate.py"


@pytest.mark.parametrize("name", sorted(name for name in ARTIFACTS if name.endswith(".svg")))
def test_svgs_are_accessible_and_self_contained(name):
    root = ET.parse(ASSETS / name).getroot()
    assert root.tag == SVG + "svg"
    assert root.attrib["role"] == "img"
    assert root.attrib["aria-labelledby"] == "title desc"
    assert root.find(SVG + "title").text
    assert root.find(SVG + "desc").text
    for node in root.iter():
        assert node.tag not in {SVG + tag for tag in ("script", "image", "foreignObject", "style")}
        assert all(not key.endswith("href") for key in node.attrib)
        if name != "preview.svg":
            assert node.tag != SVG + "text"  # the wordmarks need no font


def test_pixel_svg_uses_the_exact_master_grid():
    root = ET.parse(ASSETS / "wren-pixel.svg").getroot()
    assert root.attrib["viewBox"] == "0 0 16 16"
    group = root.find(SVG + "g")
    assert group.attrib["shape-rendering"] == "crispEdges"
    cells = set()
    for rect in group:
        assert rect.tag == SVG + "rect"
        assert rect.attrib["width"] == rect.attrib["height"] == "1"
        cells.add((int(rect.attrib["x"]), int(rect.attrib["y"])))
    assert cells == {(x, y) for y, row in enumerate(PIXELS) for x, cell in enumerate(row) if cell == "#"}


def test_terminal_blocks_preserve_both_pixels_per_cell():
    rows = (ASSETS / "wren-terminal.txt").read_text(encoding="utf-8").splitlines()
    halves = {" ": "..", "▀": "#.", "▄": ".#", "█": "##"}
    assert len(rows) == 8 and all(len(row) <= 16 for row in rows)
    decoded = []
    for row in rows:
        cells = [halves[char] for char in row.ljust(16)]
        decoded.extend("".join(cell[half] for cell in cells) for half in (0, 1))
    assert tuple(decoded) == PIXELS
    assert "\x1b" not in "".join(rows)  # no ANSI or fixed terminal colours


def test_documented_terminal_preview_matches_the_real_asset():
    docs = (ASSETS / "README.md").read_text(encoding="utf-8")
    terminal = (ASSETS / "wren-terminal.txt").read_text(encoding="utf-8")
    assert f"```text\n{terminal}```" in docs


def test_ascii_is_a_proportion_correct_fallback():
    data = (ASSETS / "wren-ascii.txt").read_bytes()
    rows = data.decode("ascii").splitlines()
    assert len(rows) == 14  # empty top/bottom border rows are omitted
    for expected, row in zip(PIXELS[1:-1], rows):
        assert len(row) <= 32
        assert row.ljust(32) == "".join("##" if cell == "#" else "  " for cell in expected)


@pytest.mark.parametrize("size", (16, 32, 128))
def test_pngs_are_transparent_nearest_neighbour_versions_of_the_grid(size):
    data = (ASSETS / f"wren-pixel-{size}.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks = []
    offset = 8
    while offset < len(data):
        length = struct.unpack("!I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        content = data[offset + 8:offset + 8 + length]
        checksum = struct.unpack("!I", data[offset + 8 + length:offset + 12 + length])[0]
        assert checksum == zlib.crc32(kind + content)
        chunks.append((kind, content))
        offset += length + 12
    assert offset == len(data)
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    assert struct.unpack("!2I5B", chunks[0][1]) == (size, size, 8, 6, 0, 0, 0)
    raw = zlib.decompress(chunks[1][1])
    stride = 1 + size * 4
    assert len(raw) == size * stride
    copper = bytes.fromhex(SOURCE["COPPER"][1:]) + b"\xff"
    for y in range(size):
        assert raw[y * stride] == 0  # no filtering
        for x in range(size):
            start = y * stride + 1 + x * 4
            expected = copper if PIXELS[y * 16 // size][x * 16 // size] == "#" else b"\x00" * 4
            assert raw[start:start + 4] == expected  # only opaque copper or transparent
