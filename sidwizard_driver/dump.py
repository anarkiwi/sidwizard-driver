"""Decoder for VICE ``sounddev=dump`` files.

Each SID write produces one space-separated decimal line:

    <cycle_delta> <irq_delta> <nmi_delta> <chipno> <addr> <byte>

The legacy 3-field shape (``cycle_delta addr byte``, no chipno) is also
accepted. Pass ``cycles_per_frame`` for NTSC (17095) or framespeed=2.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional, TextIO

PAL_CYCLES_PER_FRAME = 19656

SID_REG_MIN = 0x00
SID_REG_MAX = 0x18


@dataclass(frozen=True)
class DumpRecord:
    cycle: int
    chipno: int
    reg: int
    value: int


def iter_records(stream: Iterable[str]) -> Iterator[DumpRecord]:
    """Yield ``DumpRecord`` for each dump line in ``stream``.

    Tolerates a final partial line (VICE doesn't flush on shutdown),
    but a short line followed by more data is treated as corruption.
    """
    abs_cycle = 0
    pending_partial: Optional[str] = None
    for raw in stream:
        if pending_partial is not None:
            raise ValueError(
                f"unrecognised dump record (got "
                f"{len(pending_partial.split())} fields): {pending_partial!r}"
            )
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
            pending_partial = line
            continue
        abs_cycle += clks
        yield DumpRecord(cycle=abs_cycle, chipno=chipno, reg=addr, value=byte)


def filter_sid(
    records: Iterable[DumpRecord],
    chipno: int = 0,
    reg_min: int = SID_REG_MIN,
    reg_max: int = SID_REG_MAX,
) -> Iterator[DumpRecord]:
    for r in records:
        if r.chipno == chipno and reg_min <= r.reg <= reg_max:
            yield r


def quantise_to_frames(
    records: Iterable[DumpRecord],
    cycles_per_frame: int = PAL_CYCLES_PER_FRAME,
    start_cycle: int = 0,
) -> Iterator[tuple[int, int, int]]:
    for r in records:
        frame = (r.cycle - start_cycle) // cycles_per_frame
        yield frame, r.reg, r.value


def dedupe_consecutive(
    rows: Iterable[tuple[int, int, int]],
) -> Iterator[tuple[int, int, int]]:
    last: dict[int, int] = {}
    for frame, reg, value in rows:
        prev = last.get(reg)
        if prev == value:
            continue
        last[reg] = value
        yield frame, reg, value


def write_csv(rows: Iterable[tuple[int, int, int]], out: TextIO) -> int:
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
    drop_pre_anchor: bool = True,
    max_frame: Optional[int] = None,
) -> int:
    """Decode a dump file to CSV. Returns row count.

    ``drop_pre_anchor`` discards negative frame numbers when
    ``start_cycle > 0``. ``max_frame`` caps frames > N (trims writes
    that leak in during container shutdown).
    """

    def _drop_negative(rows):
        for frame, reg, value in rows:
            if frame >= 0:
                yield frame, reg, value

    def _cap(rows, cap):
        for frame, reg, value in rows:
            if frame > cap:
                return
            yield frame, reg, value

    with open(path) as fp:
        records = iter_records(fp)
        records = filter_sid(records, chipno=chipno)
        rows = quantise_to_frames(
            records, cycles_per_frame=cycles_per_frame, start_cycle=start_cycle
        )
        if drop_pre_anchor and start_cycle > 0:
            rows = _drop_negative(rows)
        if max_frame is not None:
            rows = _cap(rows, max_frame)
        if dedup:
            rows = dedupe_consecutive(rows)
        return write_csv(rows, out)
