"""Offline tests for the player-ghost-register dumper."""

from __future__ import annotations

import csv
from contextlib import contextmanager

import pytest

from sidwizard_driver.ghost_dump import (
    PER_VOICE_STRIDE,
    PLAYER_ENTRY,
    ZP_DUMP_END,
    ZP_DUMP_START,
    _dump_loop,
    _parse_args,
    _write_csv,
    annotated_name,
)


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
    assert args.image == "asid-vice:latest"
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
    """Minimal BinMon stand-in for ``_dump_loop``: records run_until_pc
    targets and serves canned ``mem_get`` payloads in order."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
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
        return self._payloads.pop(0)


def test_dump_loop_halts_at_player_entry_each_frame():
    """``_dump_loop`` must request a halt at PLAYER_ENTRY once per frame
    and read the configured ZP window each time."""
    width = ZP_DUMP_END - ZP_DUMP_START + 1
    payloads = [bytes((i + frame) & 0xFF for i in range(width)) for frame in range(3)]
    bm = _FakeBinMon(payloads)
    snaps = _dump_loop(bm, frames=3)

    assert bm.pc_targets == [PLAYER_ENTRY, PLAYER_ENTRY, PLAYER_ENTRY]
    assert bm.mem_calls == [(ZP_DUMP_START, ZP_DUMP_END)] * 3
    assert bm.halted_calls == 3
    assert [frame for frame, _ in snaps] == [0, 1, 2]
    assert all(len(buf) == width for _, buf in snaps)
    assert snaps[0][1] == payloads[0]


def test_dump_loop_zero_frames_is_noop():
    bm = _FakeBinMon([])
    assert _dump_loop(bm, frames=0) == []
    assert bm.pc_targets == []


def test_write_csv_no_annotate(tmp_path):
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
    # First data row should be (frame=0, addr=ZP_DUMP_START, value=0).
    assert body[0] == ["0", str(ZP_DUMP_START), "0"]
    # Last frame-0 row should be at addr = ZP_DUMP_END.
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
    # FREQLO lives at $10 (v0) — first row, value zero, label populated.
    assert body[0] == ["0", str(ZP_DUMP_START), "0", "FREQLO_v0"]
    # $25 is the only unlabelled byte in the first gap.
    gap_row = next(r for r in body if int(r[1]) == 0x25)
    assert gap_row[3] == ""
