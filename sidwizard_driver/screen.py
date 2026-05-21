"""SCREEN_GET response parsing and screencode → ASCII conversion.

A SCREEN_GET response body (opcode 0x77) is exactly 4072 bytes:
  - 24-byte header (vic_mode, charset_kind, addresses, registers, …)
  - 4048-byte payload (1000 screen RAM + 1000 color RAM + 2048 charset)

Layout copied verbatim from the asid-vice README. We do not parse charset
bytes — for printable text we use the standard screen-code → PETSCII map
keyed on charset_kind. defMON runs in normal text mode with the upper/
graphics ROM charset (charset_kind == 0).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

VIC_MODE_TEXT = 0
VIC_MODE_MC_TEXT = 1
VIC_MODE_HIRES_BITMAP = 2
VIC_MODE_MC_BITMAP = 3
VIC_MODE_EXT_TEXT = 4

CHARSET_ROM_UPPER = 0
CHARSET_ROM_LOWER = 1
CHARSET_RAM = 2

ROWS = 25
COLS = 40
SCREEN_BYTES = ROWS * COLS  # 1000
COLOR_BYTES = ROWS * COLS  # 1000
CHARSET_BYTES = 2048
HEADER_BYTES = 24
PAYLOAD_BYTES = SCREEN_BYTES + COLOR_BYTES + CHARSET_BYTES  # 4048
TOTAL_BYTES = HEADER_BYTES + PAYLOAD_BYTES  # 4072


@dataclass
class ScreenSnapshot:
    vic_mode: int
    rows: int
    cols: int
    charset_kind: int
    vic_bank: int
    border_color: int
    bg_color: tuple[int, int, int, int]
    d011: int
    d016: int
    d018: int
    screen_addr: int
    charset_addr: int
    bitmap_addr: int
    payload_len: int
    screen: bytes  # screen-codes, ROWS*COLS
    color: bytes  # low nibble = fg colour, ROWS*COLS
    charset: bytes  # 2 KiB; either ROM charset or live RAM bitmap area

    # ---- convenience -----------------------------------------------------

    def cell(self, row: int, col: int) -> int:
        return self.screen[row * self.cols + col]

    def color_at(self, row: int, col: int) -> int:
        return self.color[row * self.cols + col] & 0x0F

    def text(self) -> str:
        """Render the screen as 25 lines of ASCII (lossy: '.' for unmappable)."""
        rows: list[str] = []
        for r in range(self.rows):
            chars = [
                screencode_to_ascii(self.screen[r * self.cols + c], self.charset_kind)
                for c in range(self.cols)
            ]
            rows.append("".join(chars))
        return "\n".join(rows)

    def lines(self) -> list[str]:
        return self.text().split("\n")

    def find_text(self, needle: str) -> tuple[int, int] | None:
        """Return (row, col) of the first occurrence of needle (case-sensitive),
        or None. Searches the rendered ASCII grid."""
        up_needle = needle.upper()
        for r, line in enumerate(self.lines()):
            i = line.upper().find(up_needle)
            if i != -1:
                return (r, i)
        return None

    def contains(self, needle: str) -> bool:
        return self.find_text(needle) is not None

    def __str__(self) -> str:
        return self.text()


def parse_screen_response(body: bytes) -> ScreenSnapshot:
    if len(body) < HEADER_BYTES:
        raise ValueError(f"screen response too short: {len(body)} bytes")
    h = body[:HEADER_BYTES]
    payload_len = struct.unpack("<I", h[20:24])[0]
    payload = body[HEADER_BYTES : HEADER_BYTES + payload_len]
    if len(payload) < PAYLOAD_BYTES:
        # Tolerate truncation only by raising — caller must fix.
        raise ValueError(f"screen payload short: {len(payload)} of {PAYLOAD_BYTES}")
    screen = payload[:SCREEN_BYTES]
    color = payload[SCREEN_BYTES : SCREEN_BYTES + COLOR_BYTES]
    charset = payload[SCREEN_BYTES + COLOR_BYTES : SCREEN_BYTES + COLOR_BYTES + CHARSET_BYTES]
    return ScreenSnapshot(
        vic_mode=h[0],
        rows=h[1],
        cols=h[2],
        charset_kind=h[3],
        vic_bank=h[4],
        border_color=h[5],
        bg_color=(h[6], h[7], h[8], h[9]),
        d011=h[10],
        d016=h[11],
        d018=h[12],
        screen_addr=struct.unpack("<H", h[14:16])[0],
        charset_addr=struct.unpack("<H", h[16:18])[0],
        bitmap_addr=struct.unpack("<H", h[18:20])[0],
        payload_len=payload_len,
        screen=screen,
        color=color,
        charset=charset,
    )


# ---- screen-code → ASCII -------------------------------------------------


def screencode_to_ascii(code: int, charset_kind: int = CHARSET_ROM_UPPER) -> str:
    """Map a single C64 screen code to a printable ASCII char.

    Reverse video (high bit set) is folded onto the low 7 bits. Codes with
    no clean ASCII equivalent (line graphics, blocks, …) become '.'.

    The mapping differs slightly between the upper/graphics ROM and the
    upper/lower ROM:
      - upper/graphics (kind 0): 1..26 = A..Z
      - upper/lower   (kind 1): 1..26 = a..z; 65..90 = A..Z (shifted)
    """
    c = code & 0x7F  # ignore reverse-video bit
    if c == 0:
        return "@"
    if 1 <= c <= 26:
        if charset_kind == CHARSET_ROM_LOWER:
            return chr(ord("a") + c - 1)
        return chr(ord("A") + c - 1)
    if c == 27:
        return "["
    if c == 28:
        return "\\"  # POUND-symbol slot — closest ASCII
    if c == 29:
        return "]"
    if c == 30:
        return "^"
    if c == 31:
        return "_"
    if 32 <= c <= 63:
        # space, !, ", #, $, %, &, ', (, ), *, +, ',', -, ., /, 0..9, :, ;, <, =, >, ?
        return chr(c)
    if charset_kind == CHARSET_ROM_LOWER and 65 <= c <= 90:
        return chr(c)  # shifted upper-case in lowercase ROM
    return "."


def hex_grid(snap: ScreenSnapshot) -> Iterator[str]:
    """Yield 25 hex lines of screen RAM (40 bytes / 80 hex chars per line)."""
    for r in range(snap.rows):
        row = snap.screen[r * snap.cols : (r + 1) * snap.cols]
        yield row.hex()
