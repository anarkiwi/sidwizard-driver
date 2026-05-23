"""Offline tests for :class:`Sidwizard` — TUNEHEADER + wasjamm discovery,
SID clear / editor-mute API."""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from sidwizard_driver.sidwizard import (
    EDITOR_SCAN_HI,
    EDITOR_SCAN_LO,
    SWM_MAGIC,
    VOICE_STRIDE,
    WASJAMM_PATTERN_HEAD,
    WASJAMM_PATTERN_TAIL,
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


# ---- wasjamm discovery + editor-mute -----------------------------------


def _ram_with_wasjamm(target: int, offset: int = 0x2200) -> bytes:
    """Build a fake editor-RAM dump with the assembled resetJamIns loop
    embedded at the chosen offset. ``target`` is the wasjamm base
    address that the pattern will encode."""
    size = EDITOR_SCAN_HI - EDITOR_SCAN_LO
    buf = bytearray(b"\xea" * size)  # NOPs — must not match the wasjamm
    # pattern bytes A9 00 9D ... 8A 38 E9 07 AA 10.
    sig = (
        bytes(WASJAMM_PATTERN_HEAD)
        + bytes([target & 0xFF, (target >> 8) & 0xFF])
        + bytes(WASJAMM_PATTERN_TAIL)
    )
    buf[offset : offset + len(sig)] = sig
    return bytes(buf)


def _make_sw(mem_get_payload: bytes | None = None) -> tuple[Sidwizard, MagicMock]:
    bm = MagicMock()
    if mem_get_payload is not None:
        bm.mem_get.return_value = mem_get_payload
    return Sidwizard(bm), bm


def test_discover_wasjamm_finds_unique_signature():
    sw, _ = _make_sw(_ram_with_wasjamm(0xA4D2))
    assert sw.discover_wasjamm() == 0xA4D2


def test_discover_wasjamm_raises_when_missing():
    sw, _ = _make_sw(b"\xea" * (EDITOR_SCAN_HI - EDITOR_SCAN_LO))
    with pytest.raises(SidwizardError, match="resetJamIns signature not found"):
        sw.discover_wasjamm()


def test_discover_wasjamm_raises_on_ambiguous_addresses():
    """If two distinct addresses match the pattern, refuse rather than
    pick one — pattern uniqueness is part of the safety contract."""
    base = bytearray(_ram_with_wasjamm(0xA4D2, offset=0x2200))
    # Plant a second match encoding a different address.
    decoy_addr = 0xB100
    sig = (
        bytes(WASJAMM_PATTERN_HEAD)
        + bytes([decoy_addr & 0xFF, (decoy_addr >> 8) & 0xFF])
        + bytes(WASJAMM_PATTERN_TAIL)
    )
    base[0x4000 : 0x4000 + len(sig)] = sig
    sw, _ = _make_sw(bytes(base))
    with pytest.raises(SidwizardError, match="signature is ambiguous"):
        sw.discover_wasjamm()


def test_discover_wasjamm_dedupes_duplicate_match():
    """The same wasjamm address appearing twice (e.g. multiple
    per-voice-zero-loops in the editor build) is fine — collapse."""
    base = bytearray(_ram_with_wasjamm(0xA4D2, offset=0x2200))
    sig = (
        bytes(WASJAMM_PATTERN_HEAD)
        + bytes([0xD2, 0xA4])  # same address
        + bytes(WASJAMM_PATTERN_TAIL)
    )
    base[0x4000 : 0x4000 + len(sig)] = sig
    sw, _ = _make_sw(bytes(base))
    assert sw.discover_wasjamm() == 0xA4D2


def test_discover_wasjamm_rejects_address_outside_editor_range():
    """A pattern match whose encoded address points at ROM/IO must not
    be returned — wasjamm lives in editor RAM. Treat as no match."""
    size = EDITOR_SCAN_HI - EDITOR_SCAN_LO
    buf = bytearray(b"\xea" * size)
    bad_addr = 0xE000  # KERNAL ROM
    sig = (
        bytes(WASJAMM_PATTERN_HEAD)
        + bytes([bad_addr & 0xFF, (bad_addr >> 8) & 0xFF])
        + bytes(WASJAMM_PATTERN_TAIL)
    )
    buf[0x2200 : 0x2200 + len(sig)] = sig
    sw, _ = _make_sw(bytes(buf))
    with pytest.raises(SidwizardError, match="resetJamIns signature not found"):
        sw.discover_wasjamm()


def test_clear_sid_registers_zeros_d400_through_d418():
    sw, bm = _make_sw()
    sw.clear_sid_registers()
    bm.mem_set.assert_called_once_with(0xD400, b"\x00" * 0x19)


def test_mute_editor_voices_writes_wasjamm_slots_and_sid():
    sw, bm = _make_sw(_ram_with_wasjamm(0xA4D2))
    addr = sw.mute_editor_voices()
    assert addr == 0xA4D2
    # 3 per-voice wasjamm writes + 1 SID clear = 4 mem_set calls.
    assert bm.mem_set.call_args_list == [
        call(0xA4D2 + 0 * VOICE_STRIDE, b"\x00"),
        call(0xA4D2 + 1 * VOICE_STRIDE, b"\x00"),
        call(0xA4D2 + 2 * VOICE_STRIDE, b"\x00"),
        call(0xD400, b"\x00" * 0x19),
    ]


def test_mute_editor_voices_clear_sid_false_omits_sid_write():
    sw, bm = _make_sw(_ram_with_wasjamm(0xA4D2))
    sw.mute_editor_voices(clear_sid=False)
    assert bm.mem_set.call_args_list == [
        call(0xA4D2 + 0 * VOICE_STRIDE, b"\x00"),
        call(0xA4D2 + 1 * VOICE_STRIDE, b"\x00"),
        call(0xA4D2 + 2 * VOICE_STRIDE, b"\x00"),
    ]


def test_mute_editor_voices_honors_voice_count():
    """Multi-SID builds have up to 12 voices; the API must support
    indexing all of them at the 7-byte per-voice stride."""
    sw, bm = _make_sw(_ram_with_wasjamm(0xA4D2))
    sw.mute_editor_voices(voice_count=6, clear_sid=False)
    expected = [call(0xA4D2 + v * VOICE_STRIDE, b"\x00") for v in range(6)]
    assert bm.mem_set.call_args_list == expected


def test_mute_editor_voices_rejects_zero_voice_count():
    sw, _ = _make_sw()
    with pytest.raises(ValueError, match="voice_count must be >= 1"):
        sw.mute_editor_voices(voice_count=0)
