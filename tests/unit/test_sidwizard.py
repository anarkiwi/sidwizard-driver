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
    SWM_MAGIC,
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


def _make_sidwizard_with_ram(ram: bytes, swm_at: dict[int, bytes] | None = None) -> Sidwizard:
    """Wrap a fake BinMon whose mem_get serves the big scan and an
    optional set of TUNEHEADER probes."""
    bm = MagicMock()

    def mem_get(start: int, end: int):
        # The big editor scan reads EDITOR_SCAN_LO..EDITOR_SCAN_HI-1.
        if start == EDITOR_SCAN_LO and end == EDITOR_SCAN_HI - 1:
            return ram
        # Otherwise it's a 4-byte SWM1 probe.
        length = end - start + 1
        if swm_at and start in swm_at:
            return swm_at[start][:length]
        return b"\x00" * length

    bm.mem_get.side_effect = mem_get
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


def test_discover_tuneheader_disambiguates_by_swm_magic():
    """SID-Wizard 1.94 has multiple KERNAL.LOAD call sites (loadtun,
    loadins, ...) with the same byte signature. Disambiguate by the
    "SWM1" magic at TUNEHEADER's first 4 bytes — the editor inits an
    empty tune at boot, so only the real TUNEHEADER points at SWM1."""
    base = bytearray(_ram_with_loadtun(0x4F00))
    # Add a decoy at a different address (e.g. loadins's call site).
    decoy_addr = 0xA480
    decoy_sig = bytes(
        [
            0xA2,
            decoy_addr & 0xFF,
            0xA0,
            (decoy_addr >> 8) & 0xFF,
            0xA9,
            0x00,
            0x20,
            0xD5,
            0xFF,
        ]
    )
    base[0x2222 : 0x2222 + len(decoy_sig)] = decoy_sig

    sw = _make_sidwizard_with_ram(
        bytes(base),
        swm_at={0x4F00: SWM_MAGIC + b"\x01\x04"},  # the real one
        # decoy at 0xA480 gets b"\x00\x00\x00\x00" by default — not SWM1
    )
    assert sw.discover_tuneheader() == 0x4F00


def test_discover_tuneheader_raises_when_no_candidate_has_swm():
    """If multiple signature matches exist but none point at SWM1
    magic, refuse rather than guessing (means the editor isn't fully
    booted yet, or our heuristic is broken)."""
    base = bytearray(_ram_with_loadtun(0x4F00))
    decoy_sig = bytes([0xA2, 0x80, 0xA0, 0xA4, 0xA9, 0x00, 0x20, 0xD5, 0xFF])
    base[0x2222 : 0x2222 + len(decoy_sig)] = decoy_sig
    sw = _make_sidwizard_with_ram(bytes(base))  # no swm_at — all zeros
    with pytest.raises(SidwizardError, match="none point at SWM1"):
        sw.discover_tuneheader()


def test_discover_tuneheader_raises_on_multiple_swm_candidates():
    """If two candidates BOTH look valid (both point at SWM1), refuse
    rather than guessing — this would indicate a duplicate tune buffer
    or a stale build of the editor."""
    base = bytearray(_ram_with_loadtun(0x4F00))
    decoy_sig = bytes([0xA2, 0x00, 0xA0, 0x50, 0xA9, 0x00, 0x20, 0xD5, 0xFF])
    base[0x2222 : 0x2222 + len(decoy_sig)] = decoy_sig
    sw = _make_sidwizard_with_ram(
        bytes(base),
        swm_at={0x4F00: SWM_MAGIC, 0x5000: SWM_MAGIC},
    )
    with pytest.raises(SidwizardError, match="multiple candidates"):
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
