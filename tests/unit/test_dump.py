"""Offline tests for the sounddev=dump decoder."""

from __future__ import annotations

import io

import pytest

from sidwizard_driver.dump import (
    PAL_CYCLES_PER_FRAME,
    DumpRecord,
    decode_dump_file,
    dedupe_consecutive,
    filter_sid,
    iter_records,
    quantise_to_frames,
    write_csv,
)


def test_iter_records_dump2_format():
    """dump2 records have 6 fields: clks irq nmi chipno addr byte."""
    body = "\n".join(
        [
            "1000 50 0 0 4 15",  # cycle delta 1000, chip 0, reg 4, val 15
            "200 250 0 0 5 0",  # cycle delta 200 (cum 1200)
            "300 550 0 0 0 100",  # cycle delta 300 (cum 1500)
        ]
    )
    records = list(iter_records(io.StringIO(body)))
    assert records == [
        DumpRecord(cycle=1000, chipno=0, reg=4, value=15),
        DumpRecord(cycle=1200, chipno=0, reg=5, value=0),
        DumpRecord(cycle=1500, chipno=0, reg=0, value=100),
    ]


def test_iter_records_dump_legacy_3field():
    body = "1000 4 15\n500 5 0\n"
    records = list(iter_records(io.StringIO(body)))
    assert records == [
        DumpRecord(cycle=1000, chipno=0, reg=4, value=15),
        DumpRecord(cycle=1500, chipno=0, reg=5, value=0),
    ]


def test_iter_records_skips_blank_and_comments():
    body = "\n# leading comment\n100 0 0 0 4 1\n\n# midfile\n200 0 0 0 4 2\n"
    records = list(iter_records(io.StringIO(body)))
    assert [r.value for r in records] == [1, 2]


def test_iter_records_rejects_short_line_mid_stream():
    """A line with the wrong field count followed by valid lines is a
    real corruption (not a trailing truncation) — surface it."""
    body = "100 4 5\n100 0 0 0 4 5\n"  # 4-field line is OK here actually
    # Use 4 fields (neither legacy nor dump2) followed by a valid line
    body = "100 0 4 5\n100 0 0 0 4 5\n"
    with pytest.raises(ValueError, match="unrecognised dump record"):
        list(iter_records(io.StringIO(body)))


def test_iter_records_tolerates_truncated_trailing_line():
    """VICE's dump driver doesn't flush on shutdown, so the final
    record can be a partial fprintf. Tolerate it as end-of-stream."""
    body = "100 0 0 0 4 65\n200 0 0 0 4 70\n282 1194"  # trailing partial
    records = list(iter_records(io.StringIO(body)))
    assert len(records) == 2
    assert records[-1].value == 70


def test_filter_sid_drops_other_chip():
    recs = [
        DumpRecord(cycle=100, chipno=0, reg=4, value=1),
        DumpRecord(cycle=200, chipno=1, reg=4, value=2),
        DumpRecord(cycle=300, chipno=0, reg=4, value=3),
    ]
    out = list(filter_sid(iter(recs), chipno=0))
    assert [r.value for r in out] == [1, 3]


def test_filter_sid_drops_out_of_range_reg():
    recs = [
        DumpRecord(cycle=100, chipno=0, reg=0x18, value=1),  # kept (max inclusive)
        DumpRecord(cycle=200, chipno=0, reg=0x19, value=2),  # dropped (mirror slot)
        DumpRecord(cycle=300, chipno=0, reg=0x00, value=3),  # kept
    ]
    out = list(filter_sid(iter(recs), chipno=0))
    assert [r.reg for r in out] == [0x18, 0x00]


def test_quantise_to_frames_uses_pal():
    recs = [
        DumpRecord(cycle=0, chipno=0, reg=0, value=1),
        DumpRecord(cycle=PAL_CYCLES_PER_FRAME - 1, chipno=0, reg=0, value=2),  # frame 0
        DumpRecord(cycle=PAL_CYCLES_PER_FRAME, chipno=0, reg=0, value=3),  # frame 1
        DumpRecord(cycle=PAL_CYCLES_PER_FRAME * 3 + 5, chipno=0, reg=0, value=4),  # frame 3
    ]
    out = list(quantise_to_frames(iter(recs)))
    assert out == [(0, 0, 1), (0, 0, 2), (1, 0, 3), (3, 0, 4)]


def test_quantise_to_frames_applies_start_cycle():
    """start_cycle pins frame 0 to a known boot landmark; records before it
    end up in negative frames, which the caller is expected to discard."""
    recs = [
        DumpRecord(cycle=500, chipno=0, reg=0, value=1),  # pre-start: frame -1
        DumpRecord(cycle=2000, chipno=0, reg=0, value=2),  # frame 0
        DumpRecord(cycle=2000 + PAL_CYCLES_PER_FRAME, chipno=0, reg=0, value=3),  # frame 1
    ]
    out = list(quantise_to_frames(iter(recs), start_cycle=2000))
    assert out == [(-1, 0, 1), (0, 0, 2), (1, 0, 3)]


def test_dedupe_consecutive_collapses_same_value():
    """A player that re-writes ENV_REG=0x80 every frame produces hundreds
    of identical rows; dedup keeps only changes per register."""
    rows = [
        (0, 5, 0x80),
        (1, 5, 0x80),
        (2, 5, 0x80),
        (3, 5, 0x81),
        (4, 5, 0x81),
        (5, 6, 0x10),  # different reg — passes through
        (6, 5, 0x82),
    ]
    out = list(dedupe_consecutive(iter(rows)))
    assert out == [(0, 5, 0x80), (3, 5, 0x81), (5, 6, 0x10), (6, 5, 0x82)]


def test_dedupe_consecutive_independent_per_register():
    """Dedup state is per-register: alternating writes to two registers
    should all survive even if values happen to be equal."""
    rows = [
        (0, 0, 0x40),
        (0, 1, 0x40),
        (1, 0, 0x40),  # same reg+value as t=0 — drops
        (1, 1, 0x41),  # value changed for reg 1 — survives
    ]
    out = list(dedupe_consecutive(iter(rows)))
    assert out == [(0, 0, 0x40), (0, 1, 0x40), (1, 1, 0x41)]


def test_write_csv_emits_header_and_rows():
    rows = [(0, 4, 15), (1, 5, 0), (2, 0, 100)]
    buf = io.StringIO()
    n = write_csv(iter(rows), buf)
    assert n == 3
    lines = buf.getvalue().splitlines()
    assert lines[0] == "frame,reg,value"
    assert lines[1] == "0,4,15"
    assert lines[3] == "2,0,100"


def test_decode_dump_file_end_to_end(tmp_path):
    """Synthetic dump file: write SID frame 0 once, repeat once, then change
    one register and step forward a frame. Dedup keeps three rows."""
    dump = tmp_path / "trace.txt"
    body = "\n".join(
        [
            # cycle_delta irq nmi chip addr byte
            "100 0 0 0 4 0x41".replace("0x41", "65"),
            f"{PAL_CYCLES_PER_FRAME} 0 0 0 4 65",  # same value frame 1 — drops on dedup
            "10 0 0 0 5 200",  # frame 1, new reg
            f"{PAL_CYCLES_PER_FRAME} 0 0 0 4 70",  # frame 2, reg 4 changes
        ]
    )
    dump.write_text(body + "\n")
    out = io.StringIO()
    n = decode_dump_file(str(dump), out)
    assert n == 3
    lines = out.getvalue().splitlines()
    assert lines == [
        "frame,reg,value",
        "0,4,65",
        "1,5,200",
        "2,4,70",
    ]


def test_decode_dump_file_no_dedup_keeps_all(tmp_path):
    dump = tmp_path / "trace.txt"
    dump.write_text(
        "\n".join(
            [
                "100 0 0 0 4 65",
                f"{PAL_CYCLES_PER_FRAME} 0 0 0 4 65",  # same value, but dedup off
            ]
        )
        + "\n"
    )
    out = io.StringIO()
    n = decode_dump_file(str(dump), out, dedup=False)
    assert n == 2
