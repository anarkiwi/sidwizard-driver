"""Offline tests for :class:`Sidwizard` — only the parts that don't need VICE.

Covered:
* TUNEHEADER discovery via byte-pattern scan over a synthetic RAM image.

Not covered here (needs a live emulator, lives in smoke modules):
* wait_for_idle (polls a real BinMon)
* side_load_swm (writes to a real BinMon)
* play (taps a real key matrix)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sidwizard_driver.sidwizard import (
    EDITOR_SCAN_HI,
    EDITOR_SCAN_LO,
    Sidwizard,
    SidwizardError,
)


def _ram_with_loadtun(target: int, ram_lo: int = EDITOR_SCAN_LO) -> bytes:
    """Build a fake RAM dump with the loadtun byte pattern at one offset.

    Returns the bytes that `bm.mem_get(EDITOR_SCAN_LO, EDITOR_SCAN_HI-1)`
    should produce — i.e. the slice from EDITOR_SCAN_LO to EDITOR_SCAN_HI-1.
    """
    size = EDITOR_SCAN_HI - ram_lo
    buf = bytearray(b"\xea" * size)  # NOPs, won't match any signature byte
    # Embed signature at a chosen offset within the buffer.
    offset = 0x1234  # arbitrary spot well inside [LO, HI)
    sig = bytes(
        [
            0xA2,  # LDX #
            target & 0xFF,  # imm lo
            0xA0,  # LDY #
            (target >> 8) & 0xFF,  # imm hi
            0xA9,  # LDA #
            0x00,
            0x20,  # JSR
            0xD5,
            0xFF,
        ]
    )
    buf[offset : offset + len(sig)] = sig
    return bytes(buf)


def _make_sidwizard_with_ram(ram: bytes) -> Sidwizard:
    bm = MagicMock()
    bm.mem_get.return_value = ram
    return Sidwizard(bm)


def test_discover_tuneheader_finds_unique_signature():
    sw = _make_sidwizard_with_ram(_ram_with_loadtun(0x4F00))
    assert sw.discover_tuneheader() == 0x4F00


def test_discover_tuneheader_raises_when_missing():
    bm = MagicMock()
    bm.mem_get.return_value = b"\xea" * (EDITOR_SCAN_HI - EDITOR_SCAN_LO)
    sw = Sidwizard(bm)
    with pytest.raises(SidwizardError, match="loadtun signature not found"):
        sw.discover_tuneheader()


def test_discover_tuneheader_raises_on_ambiguous_match():
    """If two different TUNEHEADER values appear (e.g. an unfinished load
    where one copy of the editor has been overwritten by another), we
    refuse rather than silently picking the first."""
    base = bytearray(_ram_with_loadtun(0x4F00))
    # Splice a second signature with a DIFFERENT immediate at another offset.
    second_offset = 0x2222
    second_target = 0x5000
    sig = bytes(
        [
            0xA2,
            second_target & 0xFF,
            0xA0,
            (second_target >> 8) & 0xFF,
            0xA9,
            0x00,
            0x20,
            0xD5,
            0xFF,
        ]
    )
    base[second_offset : second_offset + len(sig)] = sig
    bm = MagicMock()
    bm.mem_get.return_value = bytes(base)
    sw = Sidwizard(bm)
    with pytest.raises(SidwizardError, match="ambiguous"):
        sw.discover_tuneheader()


def test_discover_tuneheader_dedupes_duplicate_match():
    """If the same TUNEHEADER address appears twice (e.g. some other code
    path also loads through loadtun), that's fine — collapse the dups."""
    base = bytearray(_ram_with_loadtun(0x4F00))
    sig = bytes([0xA2, 0x00, 0xA0, 0x4F, 0xA9, 0x00, 0x20, 0xD5, 0xFF])
    base[0x2222 : 0x2222 + len(sig)] = sig
    bm = MagicMock()
    bm.mem_get.return_value = bytes(base)
    sw = Sidwizard(bm)
    assert sw.discover_tuneheader() == 0x4F00
