"""C64 key matrix table — mirrors src/monitor/mon_keymatrix.c in asid-vice.

Only contains the canonical name for each (row, col); aliases (LSHIFT for
SHIFT, BACKSPACE for DEL, etc.) are looked up case-insensitively.

text_to_chords() converts a printable ASCII string into a sequence of
chords (single tap or shift+key) so a harness can type filenames into
defMON's disk menu without touching the KERNAL keyboard buffer.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

# Custom-key sentinels (RESTORE / CAPS / 4080) use negative row values.
# We don't need them for defMON's documented commands but include the
# constants for completeness.
ROW_RESTORE_1 = -3
ROW_CAPSLOCK = -4
ROW_4080COLUMN = -5


@dataclass(frozen=True)
class _Key:
    name: str
    row: int
    col: int


# Canonical (row, col, name) for the C64 keyboard matrix.
# Order matches mon_keymatrix.c:c64_keys.
_CANONICAL: tuple[_Key, ...] = (
    # Row 0: INST/DEL, RETURN, CRSR LR, F7, F1, F3, F5, CRSR UD
    _Key("INSTDEL", 0, 0),
    _Key("RETURN", 0, 1),
    _Key("CRSRLR", 0, 2),
    _Key("F7", 0, 3),
    _Key("F1", 0, 4),
    _Key("F3", 0, 5),
    _Key("F5", 0, 6),
    _Key("CRSRUD", 0, 7),
    # Row 1: 3 W A 4 Z S E LSHIFT
    _Key("3", 1, 0),
    _Key("W", 1, 1),
    _Key("A", 1, 2),
    _Key("4", 1, 3),
    _Key("Z", 1, 4),
    _Key("S", 1, 5),
    _Key("E", 1, 6),
    _Key("LSHIFT", 1, 7),
    # Row 2: 5 R D 6 C F T X
    _Key("5", 2, 0),
    _Key("R", 2, 1),
    _Key("D", 2, 2),
    _Key("6", 2, 3),
    _Key("C", 2, 4),
    _Key("F", 2, 5),
    _Key("T", 2, 6),
    _Key("X", 2, 7),
    # Row 3: 7 Y G 8 B H U V
    _Key("7", 3, 0),
    _Key("Y", 3, 1),
    _Key("G", 3, 2),
    _Key("8", 3, 3),
    _Key("B", 3, 4),
    _Key("H", 3, 5),
    _Key("U", 3, 6),
    _Key("V", 3, 7),
    # Row 4: 9 I J 0 M K O N
    _Key("9", 4, 0),
    _Key("I", 4, 1),
    _Key("J", 4, 2),
    _Key("0", 4, 3),
    _Key("M", 4, 4),
    _Key("K", 4, 5),
    _Key("O", 4, 6),
    _Key("N", 4, 7),
    # Row 5: + P L - . : @ ,
    _Key("PLUS", 5, 0),
    _Key("P", 5, 1),
    _Key("L", 5, 2),
    _Key("MINUS", 5, 3),
    _Key("PERIOD", 5, 4),
    _Key("COLON", 5, 5),
    _Key("AT", 5, 6),
    _Key("COMMA", 5, 7),
    # Row 6: pound * ; CLR/HOME RSHIFT = up-arrow /
    _Key("POUND", 6, 0),
    _Key("STAR", 6, 1),
    _Key("SEMICOLON", 6, 2),
    _Key("CLRHOME", 6, 3),
    _Key("RSHIFT", 6, 4),
    _Key("EQUALS", 6, 5),
    _Key("UPARROW", 6, 6),
    _Key("SLASH", 6, 7),
    # Row 7: 1 left-arrow CTRL 2 SPACE CBM Q RUNSTOP
    _Key("1", 7, 0),
    _Key("LEFTARROW", 7, 1),
    _Key("CTRL", 7, 2),
    _Key("2", 7, 3),
    _Key("SPACE", 7, 4),
    _Key("CBM", 7, 5),
    _Key("Q", 7, 6),
    _Key("RUNSTOP", 7, 7),
)


# Aliases (lookup names that resolve to the same row/col as a canonical key).
# Lower-case lookup is automatic; this table covers shorthand and synonyms.
_ALIASES: dict[str, str] = {
    "DEL": "INSTDEL",
    "INST": "INSTDEL",
    "BACKSPACE": "INSTDEL",
    "BS": "INSTDEL",
    "ENTER": "RETURN",
    "RTN": "RETURN",
    "CR": "CRSRLR",
    "RIGHT": "CRSRLR",
    "CD": "CRSRUD",
    "DOWN": "CRSRUD",
    "SHIFT": "LSHIFT",
    "LEFTSHIFT": "LSHIFT",
    "RIGHTSHIFT": "RSHIFT",
    "DOT": "PERIOD",
    "STERLING": "POUND",
    "TIMES": "STAR",
    "ASTERISK": "STAR",
    "SEMI": "SEMICOLON",
    "CLR": "CLRHOME",
    "HOME": "CLRHOME",
    "EQ": "EQUALS",
    "UP": "UPARROW",
    "EXPONENT": "UPARROW",
    "LEFT": "LEFTARROW",
    "SP": "SPACE",
    "COMMODORE": "CBM",
    "STOP": "RUNSTOP",
    "RUN": "RUNSTOP",
}


# Public KEY namespace: KEY.A, KEY.LSHIFT, KEY.LEFTARROW, etc.
# SimpleNamespace lets pyright treat arbitrary attribute access as Any
# rather than raising reportAttributeAccessIssue (which a plain class
# does, even when the attributes are populated dynamically below).
_NAME_TO_RC: dict[str, tuple[int, int]] = {}
_kv: dict[str, tuple[int, int]] = {}
for _k in _CANONICAL:
    _NAME_TO_RC[_k.name] = (_k.row, _k.col)
    _kv[_k.name] = (_k.row, _k.col)
for _alias, _target in _ALIASES.items():
    _NAME_TO_RC[_alias] = _NAME_TO_RC[_target]
    _kv[_alias] = _NAME_TO_RC[_target]
KEY = SimpleNamespace(**_kv)


_CANONICAL_NAMES: frozenset[str] = frozenset(k.name for k in _CANONICAL)


def lookup(name: str) -> tuple[int, int]:
    """Resolve a symbolic key name (case-insensitive) to (row, col)."""
    rc = _NAME_TO_RC.get(name.upper())
    if rc is None:
        raise KeyError(f"unknown key name: {name!r}")
    return rc


def canonical_name(name: str) -> str:
    """Resolve a key name (case-insensitive, alias-aware) to its canonical
    form. Raises KeyError if unknown."""
    up = name.upper()
    up = _ALIASES.get(up, up)
    if up not in _CANONICAL_NAMES:
        raise KeyError(f"unknown key name: {name!r}")
    return up


def chord_to_keys(*names: str) -> list[tuple[int, int]]:
    """Convert symbolic key names to a list of (row, col) ready for keymatrix_tap."""
    return [lookup(n) for n in names]


# ---- typing strings via the matrix --------------------------------------
#
# defMON's disk-menu prompt accepts plain key matrix scans (it does not go
# via the KERNAL ASCII layer). Only PETSCII characters that map to a single
# key (optionally with SHIFT) are typeable this way.

# Map each printable ASCII character to a list of key-names whose matrix
# bits must be set together to produce that screen character on a stock
# C64. None of these need C= (CBM) — they are the standard PETSCII typing
# layout. SHIFT+letter on a C64 with the upper/graphics charset produces
# graphic chars, not lower-case letters; defMON file names use uppercase
# letters which appear when the unshifted letter key is pressed.

_TYPE_TABLE: dict[str, tuple[str, ...]] = {
    " ": ("SPACE",),
    "A": ("A",),
    "B": ("B",),
    "C": ("C",),
    "D": ("D",),
    "E": ("E",),
    "F": ("F",),
    "G": ("G",),
    "H": ("H",),
    "I": ("I",),
    "J": ("J",),
    "K": ("K",),
    "L": ("L",),
    "M": ("M",),
    "N": ("N",),
    "O": ("O",),
    "P": ("P",),
    "Q": ("Q",),
    "R": ("R",),
    "S": ("S",),
    "T": ("T",),
    "U": ("U",),
    "V": ("V",),
    "W": ("W",),
    "X": ("X",),
    "Y": ("Y",),
    "Z": ("Z",),
    "0": ("0",),
    "1": ("1",),
    "2": ("2",),
    "3": ("3",),
    "4": ("4",),
    "5": ("5",),
    "6": ("6",),
    "7": ("7",),
    "8": ("8",),
    "9": ("9",),
    # Punctuation that is on a single key:
    "+": ("PLUS",),
    "-": ("MINUS",),
    ".": ("PERIOD",),
    ",": ("COMMA",),
    ":": ("COLON",),
    ";": ("SEMICOLON",),
    "@": ("AT",),
    "*": ("STAR",),
    "/": ("SLASH",),
    "=": ("EQUALS",),
    # SHIFT-modified characters:
    "!": ("LSHIFT", "1"),
    '"': ("LSHIFT", "2"),
    "#": ("LSHIFT", "3"),
    "$": ("LSHIFT", "4"),
    "%": ("LSHIFT", "5"),
    "&": ("LSHIFT", "6"),
    "'": ("LSHIFT", "7"),
    "(": ("LSHIFT", "8"),
    ")": ("LSHIFT", "9"),
    "<": ("LSHIFT", "COMMA"),
    ">": ("LSHIFT", "PERIOD"),
    "?": ("LSHIFT", "SLASH"),
    "[": ("LSHIFT", "COLON"),
    "]": ("LSHIFT", "SEMICOLON"),
}


def text_to_chords(text: str) -> list[tuple[str, ...]]:
    """Convert text to a sequence of key-name chords for sequential tapping.

    Lowercase letters are upper-cased (defMON shows uppercase; on the C64
    upper/graphics charset, an unshifted letter key already produces an
    uppercase glyph). Raises KeyError on a character with no matrix path.
    """
    out: list[tuple[str, ...]] = []
    for ch in text.upper():
        chord = _TYPE_TABLE.get(ch)
        if chord is None:
            raise KeyError(f"no matrix mapping for character {ch!r}")
        out.append(chord)
    return out
