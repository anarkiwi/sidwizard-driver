"""Offline tests for the player-ghost-register dumper."""

from __future__ import annotations

import csv
from contextlib import contextmanager

import pytest

from sidwizard_driver.ghost_dump import (
    DETUNER_LABELS,
    PER_VOICE_STRIDE,
    PLAYER_ENTRY,
    SELFMOD_LABELS,
    SELFMOD_SCAN_LEN,
    SELFMOD_SCAN_START,
    ZP_DUMP_END,
    ZP_DUMP_START,
    _dump_loop,
    _parse_args,
    _write_csv,
    annotated_name,
    find_detuner_base_addr,
    find_filter_selfmod_addrs,
)

# 18-byte filter-program self-modifying signature placed at known
# offsets inside a synthetic "player code" block — same byte layout the
# real SID-Wizard 1.94 single-SID build emits inside FilterProgram. We
# fill the rest of the block with $00 (BRK) so any false-positive scan
# would have to re-form the entire signature.


def _make_filter_signature(
    fltctrl_val: int = 0x55,
    fltposi_val: int = 0x66,
    cwepcnt_val: int = 0x77,
    inc_target: int = 0,  # caller fills with the expected CWEPCNT addr
) -> bytes:
    """Build the 18-byte FilterProgram instruction sequence with the
    three self-modifying operand values + a trailing ``inc abs`` whose
    target is the CWEPCNT operand byte. ``inc_target`` is the absolute
    address of CWEPCNT+1 inside the host memory block."""
    return bytes(
        [
            0xE0,
            fltctrl_val,  # cpx #imm (FLTCTRL)
            0xD0,
            0x10,  # bne SwUpEnd (dummy +$10)
            0xA0,
            fltposi_val,  # ldy #imm (FLTPOSI)
            0xB1,
            0xFB,  # lda (PLAYERZP),y
            0x30,
            0x10,  # bmi NOCWEEP
            0xC8,  # iny (FISWEEP)
            0xC9,
            cwepcnt_val,  # cmp #imm (CWEPCNT)
            0xF0,
            0x10,  # beq FLADVAN
            0xEE,
            inc_target & 0xFF,
            (inc_target >> 8) & 0xFF,  # inc abs
        ]
    )


def _embed_signature(
    base_addr: int,
    offset: int,
    block_len: int = 0x1000,
    **sig_kwargs,
) -> bytes:
    """Place the signature inside a zeroed block. ``inc_target`` is set
    automatically so the self-consistency check inside
    :func:`find_filter_selfmod_addrs` passes."""
    block = bytearray(block_len)
    # CWEPCNT operand lives at offset+12 (signature byte 12).
    cwepcnt_addr = base_addr + offset + 12
    sig = _make_filter_signature(inc_target=cwepcnt_addr, **sig_kwargs)
    block[offset : offset + len(sig)] = sig
    return bytes(block)


def test_annotated_name_first_block_v0_v1_v2():
    """FREQLO sits at the start of the per-voice block, so the +0 / +7 /
    +14 stride must report ``FREQLO_v{0,1,2}`` with no ambiguity."""
    assert annotated_name(0x10) == "FREQLO_v0"
    assert annotated_name(0x10 + PER_VOICE_STRIDE) == "FREQLO_v1"
    assert annotated_name(0x10 + 2 * PER_VOICE_STRIDE) == "FREQLO_v2"


def test_annotated_name_second_block_strides():
    """PACKCNT lives in the second block; its v1 slot is $26+7 = $2D."""
    assert annotated_name(0x26) == "PACKCNT_v0"
    assert annotated_name(0x26 + PER_VOICE_STRIDE) == "PACKCNT_v1"


def test_annotated_name_unknown_returns_empty():
    """Bytes outside any per-voice stride window report no label."""
    # $25 falls in the gap between the FREQLO block (v2 ends $24) and
    # the PACKCNT base at $26 — no voice slot maps to it.
    assert annotated_name(0x25) == ""
    # $7F is well outside any labelled region.
    assert annotated_name(0x7F) == ""


def test_parse_args_defaults():
    args = _parse_args(["--d64", "a.d64", "--swm", "b.swm", "--out", "out.csv"])
    assert args.d64 == "a.d64"
    assert args.swm == "b.swm"
    assert args.out == "out.csv"
    assert args.frames == 60
    assert args.annotate is False
    assert args.image == "anarkiwi/headlessvice:latest"
    assert args.port == 6502


def test_parse_args_overrides():
    args = _parse_args(
        [
            "--d64",
            "a.d64",
            "--swm",
            "b.swm",
            "--out",
            "c.csv",
            "--frames",
            "120",
            "--annotate",
            "--port",
            "1234",
        ]
    )
    assert args.frames == 120
    assert args.annotate is True
    assert args.port == 1234


def test_parse_args_requires_d64_swm_out():
    with pytest.raises(SystemExit):
        _parse_args([])


class _FakeBinMon:
    """Minimal BinMon stand-in. ``mem_get_responder`` is a callable
    ``(start, end) -> bytes`` that produces the bytes for each call —
    lets a test stage one block as "the player code" and another as
    "the ZP window" without queueing fragile per-call payloads.
    """

    def __init__(self, mem_get_responder):
        self._responder = mem_get_responder
        self.pc_targets: list[int] = []
        self.mem_calls: list[tuple[int, int]] = []
        self.halted_calls = 0

    def run_until_pc(self, target):
        self.pc_targets.append(target)

    @contextmanager
    def halted(self):
        self.halted_calls += 1
        yield

    def mem_get(self, start, end):
        self.mem_calls.append((start, end))
        return self._responder(start, end)


def _filter_signature_offset() -> int:
    """A fixed in-block offset to place the signature. Picked so the
    resulting absolute addresses fall well inside the scan window."""
    return 0x40


def _wrpitch_bytes_zp(detuner_zp: int, continuation: int = 0x9D) -> bytes:
    """``WRPITCH`` opening sequence in the zp,X variant::

    B5 10           ; lda FREQLO,X
    75 ??           ; adc DETUNER,X        (?? = DETUNER ZP addr)
    XX              ; continuation: $9D (sta abs,X) or $08 (php)
    """
    return bytes([0xB5, 0x10, 0x75, detuner_zp & 0xFF, continuation])


def _wrpitch_bytes_abs(detuner_addr: int, continuation: int = 0x9D) -> bytes:
    """``WRPITCH`` opening sequence in the abs,X variant — the form
    SID-Wizard 1.94's compiled editor build emits when DETUNER lives
    in the player-code area instead of zero page::

        B5 10           ; lda FREQLO,X
        7D LO HI        ; adc DETUNER,X  (abs,X — DETUNER at $LOHI)
        XX              ; continuation: $9D or $08
    """
    return bytes(
        [
            0xB5,
            0x10,
            0x7D,
            detuner_addr & 0xFF,
            (detuner_addr >> 8) & 0xFF,
            continuation,
        ]
    )


def _wrpitch_bytes(detuner_zp: int, continuation: int = 0x9D) -> bytes:
    """Backwards-compatible alias for the zp,X variant — kept so the
    existing ``_make_responder`` setup keeps working without churn."""
    return _wrpitch_bytes_zp(detuner_zp, continuation)


def _addfreq_bytes(freqmod_zp: int = 0x50) -> bytes:
    """``ADDFREQ`` opening — shares the ``B5 10 75 ??`` prefix with
    WRPITCH but writes back to ZP via ``$95``. Discovery must NOT
    pick this up as DETUNER."""
    return bytes([0xB5, 0x10, 0x75, freqmod_zp & 0xFF, 0x95, 0x10])


def _make_responder(
    zp_payload_fn,
    fltctrl_val,
    fltposi_val,
    cwepcnt_val,
    detuner_zp_base: int = 0x70,
    detuner_vals: tuple[int, int, int] = (0x01, 0x02, 0x03),
):
    """Build a mem_get responder that serves:

    * the player-code scan (FilterProgram signature + WRPITCH signature),
    * the per-frame ZP window,
    * the three per-frame self-mod operand byte reads, and
    * the three per-frame DETUNER (per-voice) byte reads.
    """
    sm_offset = _filter_signature_offset()
    wrpitch_offset = sm_offset + 32  # well clear of the filter signature
    code_block = bytearray(
        _embed_signature(
            SELFMOD_SCAN_START,
            sm_offset,
            fltctrl_val=fltctrl_val,
            fltposi_val=fltposi_val,
            cwepcnt_val=cwepcnt_val,
        )
    )
    # Drop the WRPITCH B5 10 75 ?? pattern at a distinct offset.
    wp = _wrpitch_bytes(detuner_zp_base)
    code_block[wrpitch_offset : wrpitch_offset + len(wp)] = wp

    fltctrl_addr = SELFMOD_SCAN_START + sm_offset + 1
    fltposi_addr = SELFMOD_SCAN_START + sm_offset + 5
    cwepcnt_addr = SELFMOD_SCAN_START + sm_offset + 12
    sm_vals = {
        fltctrl_addr: fltctrl_val,
        fltposi_addr: fltposi_val,
        cwepcnt_addr: cwepcnt_val,
    }
    detuner_addrs = (
        detuner_zp_base,
        detuner_zp_base + 7,
        detuner_zp_base + 14,
    )
    det_vals = dict(zip(detuner_addrs, detuner_vals, strict=True))

    def responder(start, end):
        if start == SELFMOD_SCAN_START and end == SELFMOD_SCAN_START + SELFMOD_SCAN_LEN - 1:
            return bytes(code_block)
        if start == ZP_DUMP_START and end == ZP_DUMP_END:
            return zp_payload_fn()
        if start == end and start in sm_vals:
            return bytes([sm_vals[start]])
        if start == end and start in det_vals:
            return bytes([det_vals[start]])
        raise AssertionError(f"unexpected mem_get({start:#x}, {end:#x})")

    return responder, (fltctrl_addr, fltposi_addr, cwepcnt_addr), detuner_addrs


def test_find_filter_selfmod_addrs_recovers_three_operands():
    """The scan returns absolute addresses for FLTCTRL/FLTPOSI/CWEPCNT
    operand bytes from a synthetic player-code block."""
    base = 0x1000
    offset = 0x80
    code = _embed_signature(base, offset)
    addrs = find_filter_selfmod_addrs(code, base)
    assert addrs == (base + offset + 1, base + offset + 5, base + offset + 12)


def test_find_filter_selfmod_addrs_raises_on_missing_signature():
    """Without the 18-byte sequence the scan must error out — no
    silent fallback that would later mis-seed the player."""
    with pytest.raises(ValueError, match="signature not found"):
        find_filter_selfmod_addrs(bytes(0x800), 0x1000)


def test_find_filter_selfmod_addrs_rejects_inc_target_mismatch():
    """If the trailing ``inc abs`` points somewhere other than the
    computed CWEPCNT address, the signature is suspicious — bail
    rather than report bogus addresses."""
    base = 0x1000
    offset = 0x40
    block = bytearray(_embed_signature(base, offset))
    # Corrupt the inc-abs target so the consistency check fails.
    block[offset + 16] = 0xFF
    block[offset + 17] = 0xFF
    with pytest.raises(ValueError, match="disagrees"):
        find_filter_selfmod_addrs(bytes(block), base)


def test_find_detuner_base_addr_recovers_zp_address():
    """``find_detuner_base_addr`` reads the operand byte of WRPITCH's
    ``adc DETUNER,X`` instruction (zp,X form), which encodes the ZP
    address of DETUNER_v0."""
    base = 0x1000
    block = bytearray(0x100)
    sig = _wrpitch_bytes_zp(detuner_zp=0x7A)
    block[0x30 : 0x30 + len(sig)] = sig
    assert find_detuner_base_addr(bytes(block), base) == 0x7A


def test_find_detuner_base_addr_recovers_abs_address():
    """SID-Wizard 1.94's editor build places DETUNER in the player-code
    area (e.g. ``$1050``) instead of zero page, so WRPITCH uses abs,X
    addressing (``7D LO HI``). The discovery must read the 16-bit
    little-endian operand correctly."""
    base = 0x1010
    block = bytearray(0x800)
    sig = _wrpitch_bytes_abs(detuner_addr=0x1050)
    block[0x100 : 0x100 + len(sig)] = sig
    assert find_detuner_base_addr(bytes(block), base) == 0x1050


def test_find_detuner_base_addr_abs_accepts_php_continuation():
    """Editor + MIDI build's WRPITCH inserts ``php`` ($08) after the
    abs,X adc. Discovery must accept either $08 or $9D."""
    base = 0x1010
    block = bytearray(0x200)
    sig = _wrpitch_bytes_abs(detuner_addr=0x1050, continuation=0x08)
    block[0x80 : 0x80 + len(sig)] = sig
    assert find_detuner_base_addr(bytes(block), base) == 0x1050


def test_find_detuner_base_addr_rejects_low_zp_operand():
    """If the signature matches but the operand byte is below the
    expected CONST_VAR window ($60..$FE), the match is suspicious — bail
    rather than seed pysidwizard from a low-ZP false positive."""
    block = bytearray(0x100)
    block[0x30:0x35] = _wrpitch_bytes(detuner_zp=0x10)  # same as FREQLO_v0
    with pytest.raises(ValueError, match="outside the expected ZP range"):
        find_detuner_base_addr(bytes(block), 0x1000)


def test_find_detuner_base_addr_raises_on_missing_signature():
    with pytest.raises(ValueError, match="signature not found"):
        find_detuner_base_addr(bytes(0x800), 0x1000)


def test_find_detuner_base_addr_skips_addfreq_lookalike():
    """``ADDFREQ`` opens with ``B5 10 75 ??`` exactly like WRPITCH but
    writes back to ZP via ``95 10`` instead of an absolute store. The
    discovery must skip ADDFREQ and locate the real WRPITCH further on
    in the code — otherwise it returns FREQMODL_v0 ($50) as
    "DETUNER", which is a silent corruption with no easy diagnostic."""
    base = 0x1000
    block = bytearray(0x200)
    # ADDFREQ-shaped lookalike first (lower offset).
    block[0x20 : 0x20 + 6] = _addfreq_bytes(freqmod_zp=0x50)
    # Real WRPITCH later, with DETUNER at $7A.
    block[0x80 : 0x80 + 5] = _wrpitch_bytes(detuner_zp=0x7A)
    assert find_detuner_base_addr(bytes(block), base) == 0x7A


def test_find_detuner_base_addr_accepts_editor_php_continuation():
    """The editor build with MIDI_support inserts ``php; clc; adc
    pitchShiftLo,X`` between ``adc DETUNER,X`` and the SID store, so the
    byte after the DETUNER operand is ``$08`` (php) rather than ``$9D``
    (sta abs,X). Discovery must accept either."""
    base = 0x1000
    block = bytearray(0x100)
    sig = _wrpitch_bytes(detuner_zp=0x7C, continuation=0x08)
    block[0x40 : 0x40 + len(sig)] = sig
    assert find_detuner_base_addr(bytes(block), base) == 0x7C


def test_dump_loop_halts_at_player_entry_each_frame():
    """``_dump_loop`` halts at PLAYER_ENTRY per frame, reads the ZP
    window, and (after first-frame discovery) reads each selfmod +
    per-voice DETUNER byte."""
    width = ZP_DUMP_END - ZP_DUMP_START + 1
    counter = {"i": 0}

    def zp_payload():
        frame = counter["i"]
        counter["i"] += 1
        return bytes((i + frame) & 0xFF for i in range(width))

    responder, expected_sm, expected_det = _make_responder(
        zp_payload,
        fltctrl_val=0x0E,
        fltposi_val=0x10,
        cwepcnt_val=0x03,
        detuner_zp_base=0x7A,
        detuner_vals=(0x01, 0x02, 0x03),
    )
    bm = _FakeBinMon(responder)

    (
        zp_snaps,
        sm_addrs,
        sm_snaps,
        det_addrs,
        det_snaps,
    ) = _dump_loop(bm, frames=3)
    assert bm.pc_targets == [PLAYER_ENTRY, PLAYER_ENTRY, PLAYER_ENTRY]
    assert bm.halted_calls == 3
    assert sm_addrs == expected_sm
    assert det_addrs == expected_det
    assert [frame for frame, _ in zp_snaps] == [0, 1, 2]
    assert all(len(buf) == width for _, buf in zp_snaps)
    # All three frames see the same self-mod / DETUNER values (the
    # stubbed responder returns constants); pysidwizard only needs
    # frame 0 but the loop captures every frame for completeness.
    assert sm_snaps == [bytes([0x0E, 0x10, 0x03])] * 3
    assert det_snaps == [bytes([0x01, 0x02, 0x03])] * 3


def test_dump_loop_zero_frames_is_noop():
    bm = _FakeBinMon(lambda s, e: b"")
    zp_snaps, sm_addrs, sm_snaps, det_addrs, det_snaps = _dump_loop(bm, frames=0)
    assert zp_snaps == []
    assert sm_addrs is None
    assert sm_snaps == []
    assert det_addrs is None
    assert det_snaps == []
    assert bm.pc_targets == []


def test_write_csv_no_annotate(tmp_path):
    """ZP-only mode (no selfmod) preserves the original schema."""
    width = ZP_DUMP_END - ZP_DUMP_START + 1
    snaps = [
        (0, bytes(range(width))),
        (1, bytes((width - i - 1) for i in range(width))),
    ]
    out = tmp_path / "ghost.csv"
    rows = _write_csv(snaps, str(out), annotate=False)
    assert rows == 2 * width

    with open(out, newline="") as fp:
        reader = csv.reader(fp)
        header = next(reader)
        assert header == ["frame", "addr", "value"]
        body = list(reader)
    assert len(body) == 2 * width
    assert body[0] == ["0", str(ZP_DUMP_START), "0"]
    assert body[width - 1] == ["0", str(ZP_DUMP_END), str(width - 1)]


def test_write_csv_annotate_adds_label_column(tmp_path):
    width = ZP_DUMP_END - ZP_DUMP_START + 1
    snaps = [(0, bytes(width))]
    out = tmp_path / "ghost-annot.csv"
    rows = _write_csv(snaps, str(out), annotate=True)
    assert rows == width

    with open(out, newline="") as fp:
        reader = csv.reader(fp)
        header = next(reader)
        body = list(reader)
    assert header == ["frame", "addr", "value", "label"]
    assert body[0] == ["0", str(ZP_DUMP_START), "0", "FREQLO_v0"]
    gap_row = next(r for r in body if int(r[1]) == 0x25)
    assert gap_row[3] == ""


def test_write_csv_with_selfmod_appends_three_rows_per_frame(tmp_path):
    """When selfmod data is supplied, each frame gains exactly three
    extra rows (FLTCTRL, FLTPOSI, CWEPCNT) at their player-code
    addresses, between the ZP rows of consecutive frames."""
    width = ZP_DUMP_END - ZP_DUMP_START + 1
    snaps = [(0, bytes(width)), (1, bytes(width))]
    selfmod_addrs = (0x1041, 0x1045, 0x104C)
    selfmod_snaps = [bytes([0x0E, 0x10, 0x03]), bytes([0x0E, 0x13, 0x05])]
    out = tmp_path / "ghost-sm.csv"
    rows = _write_csv(
        snaps,
        str(out),
        annotate=True,
        selfmod_addrs=selfmod_addrs,
        selfmod_snapshots=selfmod_snaps,
    )
    expected_per_frame = width + len(SELFMOD_LABELS)
    assert rows == 2 * expected_per_frame

    with open(out, newline="") as fp:
        body = list(csv.reader(fp))[1:]

    # Each frame's last 3 rows are the selfmod rows (after the ZP).
    frame0_sm = body[width : width + 3]
    assert frame0_sm == [
        ["0", "4161", "14", "FLTCTRL"],  # $1041 = 4161
        ["0", "4165", "16", "FLTPOSI"],  # $1045
        ["0", "4172", "3", "CWEPCNT"],  # $104C
    ]
    frame1_start = expected_per_frame
    frame1_sm = body[frame1_start + width : frame1_start + width + 3]
    assert frame1_sm == [
        ["1", "4161", "14", "FLTCTRL"],
        ["1", "4165", "19", "FLTPOSI"],
        ["1", "4172", "5", "CWEPCNT"],
    ]


def test_write_csv_with_selfmod_no_annotate_omits_label(tmp_path):
    """Without ``annotate``, selfmod rows still appear but lack the
    label column (matches the ZP rows' schema)."""
    snaps = [(0, bytes(ZP_DUMP_END - ZP_DUMP_START + 1))]
    selfmod_addrs = (0x1041, 0x1045, 0x104C)
    selfmod_snaps = [bytes([1, 2, 3])]
    out = tmp_path / "ghost-sm-noannot.csv"
    _write_csv(
        snaps,
        str(out),
        annotate=False,
        selfmod_addrs=selfmod_addrs,
        selfmod_snapshots=selfmod_snaps,
    )
    with open(out, newline="") as fp:
        body = list(csv.reader(fp))[1:]
    last_three = body[-3:]
    assert [row[1:] for row in last_three] == [
        ["4161", "1"],
        ["4165", "2"],
        ["4172", "3"],
    ]


def test_write_csv_with_detuner_appends_three_more_rows_per_frame(tmp_path):
    """With both selfmod and DETUNER data, each frame gains 3+3=6 extra
    rows after the ZP block, in (FLTCTRL/FLTPOSI/CWEPCNT) then
    (DETUNER_v0/_v1/_v2) order."""
    width = ZP_DUMP_END - ZP_DUMP_START + 1
    snaps = [(0, bytes(width)), (1, bytes(width))]
    selfmod_addrs = (0x1041, 0x1045, 0x104C)
    selfmod_snaps = [bytes([0x0E, 0x10, 0x03]), bytes([0x0E, 0x13, 0x05])]
    detuner_addrs = (0x7A, 0x81, 0x88)  # base, +7, +14
    detuner_snaps = [bytes([0x01, 0x02, 0x03]), bytes([0x04, 0x05, 0x06])]
    out = tmp_path / "ghost-det.csv"
    rows = _write_csv(
        snaps,
        str(out),
        annotate=True,
        selfmod_addrs=selfmod_addrs,
        selfmod_snapshots=selfmod_snaps,
        detuner_addrs=detuner_addrs,
        detuner_snapshots=detuner_snaps,
    )
    expected_per_frame = width + len(SELFMOD_LABELS) + len(DETUNER_LABELS)
    assert rows == 2 * expected_per_frame

    with open(out, newline="") as fp:
        body = list(csv.reader(fp))[1:]

    # Frame 0's DETUNER rows come after the selfmod rows (= last 3).
    frame0_det = body[width + 3 : width + 6]
    assert frame0_det == [
        ["0", "122", "1", "DETUNER_v0"],  # $7A = 122
        ["0", "129", "2", "DETUNER_v1"],  # $81 = 129
        ["0", "136", "3", "DETUNER_v2"],  # $88 = 136
    ]
    frame1_start = expected_per_frame
    frame1_det = body[frame1_start + width + 3 : frame1_start + width + 6]
    assert frame1_det == [
        ["1", "122", "4", "DETUNER_v0"],
        ["1", "129", "5", "DETUNER_v1"],
        ["1", "136", "6", "DETUNER_v2"],
    ]


def test_write_csv_detuner_only_without_selfmod(tmp_path):
    """DETUNER rows can stand alone (selfmod_addrs=None) — they still
    write after the ZP block at the discovered ZP addresses."""
    width = ZP_DUMP_END - ZP_DUMP_START + 1
    snaps = [(0, bytes(width))]
    detuner_addrs = (0x7A, 0x81, 0x88)
    detuner_snaps = [bytes([0x11, 0x22, 0x33])]
    out = tmp_path / "ghost-det-only.csv"
    rows = _write_csv(
        snaps,
        str(out),
        annotate=True,
        detuner_addrs=detuner_addrs,
        detuner_snapshots=detuner_snaps,
    )
    assert rows == width + len(DETUNER_LABELS)
    with open(out, newline="") as fp:
        body = list(csv.reader(fp))[1:]
    assert body[-3:] == [
        ["0", "122", "17", "DETUNER_v0"],
        ["0", "129", "34", "DETUNER_v1"],
        ["0", "136", "51", "DETUNER_v2"],
    ]
