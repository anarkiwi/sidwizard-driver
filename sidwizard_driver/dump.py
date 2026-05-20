"""Decoder for VICE ``sounddev=dump`` files.

What this models
----------------
The text-format trace written by ``src/arch/shared/sounddrv/sounddump.c``
in asid-vice. The driver exposes two record shapes; for SID writes the
core invokes ``dump2``:

    dump_dump2(clks, irq_clks, nmi_clks, chipno, addr, byte)

emitting one space-separated decimal line per write:

    <cycle_delta_since_last_write> <cycle_delta_since_irq>
    <cycle_delta_since_nmi> <chipno> <addr> <byte>

``clks`` is the delta since the last SID write (``snddata.wclk``), not an
absolute cycle counter — absolute cycles must be recovered by
accumulating the delta. ``addr`` is the 5-bit SID register offset
(0..0x1F); ``chipno`` is 0 for the primary SID.

Scope
-----
* Parse the text file produced by ``-sounddev dump -soundarg <path>``.
* Convert cycle-delta records into absolute-cycle records.
* Quantise to PAL frames (``19656`` cycles/frame).
* Deduplicate consecutive writes of the same value to the same register.
* Emit ``(frame, reg, value)`` rows suitable for the pysidwizard CSV.

Not modelled
------------
* The single-arg ``dump_dump`` record shape — only ``dump2`` is observed
  when both are present (sound.c prefers dump2). We accept either by
  field count for forward-compat.
* NTSC framing (``17095`` cycles/frame). Override via ``cycles_per_frame``.
* Multi-SID chip filtering beyond ``chipno`` — caller decides which chips
  to keep.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from typing import Iterable, Iterator, TextIO

# PAL cycles per frame. SID-Wizard's player runs every PAL frame
# (one IRQ at the same scanline each refresh); pysidwizard uses the
# same constant. NTSC tunes would use 17095; out of scope for v0.
PAL_CYCLES_PER_FRAME = 19656

# SID register range we keep by default. Writes to mirror addresses
# above 0x18 (e.g. for the unused $D419..$D41F slots) are dropped.
SID_REG_MIN = 0x00
SID_REG_MAX = 0x18


@dataclass(frozen=True)
class DumpRecord:
    """One line of the dump file with deltas resolved to absolute cycle."""

    cycle: int  # absolute CPU cycle (accumulated from deltas)
    chipno: int
    reg: int
    value: int


def iter_records(stream: Iterable[str]) -> Iterator[DumpRecord]:
    """Yield ``DumpRecord`` for each dump line in ``stream``.

    Lines with 6 fields are ``dump_dump2``; lines with 3 fields are the
    legacy ``dump_dump`` (chipno=0 assumed). Blank lines and lines
    starting with ``#`` are skipped.
    """
    abs_cycle = 0
    for raw in stream:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 6:
            clks, _irq, _nmi, chipno, addr, byte = (int(p) for p in parts)
        elif len(parts) == 3:
            clks, addr, byte = (int(p) for p in parts)
            chipno = 0
        else:
            raise ValueError(f"unrecognised dump record (got {len(parts)} fields): {line!r}")
        abs_cycle += clks
        yield DumpRecord(cycle=abs_cycle, chipno=chipno, reg=addr, value=byte)


def filter_sid(
    records: Iterable[DumpRecord],
    chipno: int = 0,
    reg_min: int = SID_REG_MIN,
    reg_max: int = SID_REG_MAX,
) -> Iterator[DumpRecord]:
    """Keep only writes to the named SID chip and register range."""
    for r in records:
        if r.chipno == chipno and reg_min <= r.reg <= reg_max:
            yield r


def quantise_to_frames(
    records: Iterable[DumpRecord],
    cycles_per_frame: int = PAL_CYCLES_PER_FRAME,
    start_cycle: int = 0,
) -> Iterator[tuple[int, int, int]]:
    """Convert ``DumpRecord``s to ``(frame, reg, value)`` triples.

    ``start_cycle`` is subtracted before dividing so the caller can
    pin frame 0 to a known boot landmark (e.g. the first cycle after
    the F1 keypress).
    """
    for r in records:
        frame = (r.cycle - start_cycle) // cycles_per_frame
        yield frame, r.reg, r.value


def dedupe_consecutive(
    rows: Iterable[tuple[int, int, int]],
) -> Iterator[tuple[int, int, int]]:
    """Drop consecutive ``(frame, reg, value)`` rows that match the prior
    value for the same register, regardless of frame.

    Matches the dedup pysidwizard's ``render_wav`` does: a player that
    re-writes the same envelope value every frame produces hundreds of
    redundant rows; only the *changes* are interesting.
    """
    last: dict[int, int] = {}
    for frame, reg, value in rows:
        prev = last.get(reg)
        if prev == value:
            continue
        last[reg] = value
        yield frame, reg, value


def write_csv(rows: Iterable[tuple[int, int, int]], out: TextIO) -> int:
    """Write ``(frame, reg, value)`` rows to ``out`` as CSV. Returns the
    number of data rows written (excluding header).

    Schema matches pysidwizard's ``render_wav`` sibling CSV:
    columns are ``frame``, ``reg``, ``value`` with integer values
    (``reg`` and ``value`` in decimal — the consumer can format as hex
    if it wants)."""
    writer = csv.writer(out)
    writer.writerow(["frame", "reg", "value"])
    n = 0
    for frame, reg, value in rows:
        writer.writerow([frame, reg, value])
        n += 1
    return n


def decode_dump_file(
    path: str,
    out: TextIO,
    cycles_per_frame: int = PAL_CYCLES_PER_FRAME,
    chipno: int = 0,
    start_cycle: int = 0,
    dedup: bool = True,
) -> int:
    """High-level: open dump file, filter, quantise, dedup, write CSV.

    Returns the number of CSV data rows written.
    """
    with open(path) as fp:
        records = iter_records(fp)
        records = filter_sid(records, chipno=chipno)
        rows = quantise_to_frames(records, cycles_per_frame=cycles_per_frame, start_cycle=start_cycle)
        if dedup:
            rows = dedupe_consecutive(rows)
        return write_csv(rows, out)
