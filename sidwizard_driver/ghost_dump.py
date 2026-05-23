"""Dump SID-Wizard's per-frame player ghost-register state to CSV."""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import tempfile

from vice_driver import BinMon, DiskMount, ViceContainer, ViceContainerError

from .fetch import fetch_disk1_d64
from .sidwizard import Sidwizard, SidwizardError

log = logging.getLogger("sidwizard_driver.ghost_dump")


# Player jump table: $1000=INITER, $1003=PLAYER (per-frame entry).
PLAYER_ENTRY = 0x1003

# anarkiwi/headlessvice's x64sc needs a writable $HOME/.local/state/vice.
VICE_STATE_DIR = "/root/.local/state/vice"

# Zero-page region covering per-voice ghost-register blocks plus
# FREQMOD/vibrato state. Over-dumps a bit ($10..$80, ~0x71 bytes/frame).
ZP_DUMP_START = 0x10
ZP_DUMP_END = 0x80


# Variable name map for the per-voice zero-page region. Labels are for
# v0; v1 = +7, v2 = +14. Gaps are left unannotated but still dumped.
PLAYER_ZP_LABELS = {
    0x10: "FREQLO",
    0x11: "FREQHI",
    0x12: "PWLOGHO",
    0x13: "PWHIGHO",
    0x14: "WFGHOST",
    0x15: "PTNGATE",
    0x16: "PWEEPCNT",
    0x26: "PACKCNT",
    0x27: "SPDCNT",
    0x28: "SEQPOS",
    0x29: "PTNPOS",
    0x2A: "WFTPOS",
    0x2B: "PWTPOS",
    0x2C: "ARPSCNT",
    0x3A: "CURPTN",
    0x3B: "CURNOT",
    0x3C: "DPITCH",
    0x3D: "CURIFX",
    0x3E: "CURINS",
    0x3F: "CURFX2",
    0x40: "CURVAL",
    0x4F: "SLIDEVIB",
    0x50: "FREQMODL",
    0x51: "FREQMODH",
    0x52: "VIDELCNT",
    0x53: "VIBFREQU",
    0x54: "VIBRACNT",
    0x55: "TRANSP",
    0x68: "CURCHORD",
    0x69: "CHORDPOS",
}

PER_VOICE_STRIDE = 7
N_VOICES = 3


def annotated_name(addr: int) -> str:
    for base, name in PLAYER_ZP_LABELS.items():
        for v in range(N_VOICES):
            if base + v * PER_VOICE_STRIDE == addr:
                return f"{name}_v{v}"
    return ""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--d64", default=None, help="SID-Wizard editor .d64 (auto-fetched if omitted)")
    p.add_argument("--swm", required=True, help="SWM module to play")
    p.add_argument("--frames", type=int, default=60)
    p.add_argument("--out", required=True, help="output CSV path")
    p.add_argument("--annotate", action="store_true", help="add SID-Wizard variable names")
    p.add_argument("--image", default="anarkiwi/headlessvice:latest")
    p.add_argument("--port", type=int, default=6502)
    p.add_argument("--idle-timeout", type=float, default=60.0)
    p.add_argument("--load-timeout", type=float, default=10.0)
    p.add_argument(
        "--mute-editor",
        action="store_true",
        help="zero $D400-$D418 at the pre-F1 checkpoint",
    )
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p.parse_args(argv)


def _dump_loop(bm: BinMon, frames: int) -> list[tuple[int, bytes]]:
    snapshots: list[tuple[int, bytes]] = []
    for frame in range(frames):
        bm.run_until_pc(PLAYER_ENTRY)
        with bm.halted():
            zp = bm.mem_get(ZP_DUMP_START, ZP_DUMP_END)
        snapshots.append((frame, bytes(zp)))
    return snapshots


def _write_csv(
    snapshots: list[tuple[int, bytes]],
    out_path: str,
    annotate: bool,
) -> int:
    rows = 0
    with open(out_path, "w", newline="") as fp:
        w = csv.writer(fp)
        header = ["frame", "addr", "value"]
        if annotate:
            header.append("label")
        w.writerow(header)
        for frame, data in snapshots:
            for i, val in enumerate(data):
                addr = ZP_DUMP_START + i
                row: list[object] = [frame, addr, val]
                if annotate:
                    row.append(annotated_name(addr))
                w.writerow(row)
                rows += 1
    return rows


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - live VICE
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if not args.d64:
        args.d64 = str(fetch_disk1_d64())
    if not os.path.isfile(args.d64):
        print(f"not a file: {args.d64}", file=sys.stderr)
        return 2
    if not os.path.isfile(args.swm):
        print(f"not a file: {args.swm}", file=sys.stderr)
        return 2

    host_work_dir = tempfile.mkdtemp(prefix="sidwizard-driver-ghost-")
    container_work_dir = "/tmp/sidwizard-driver"
    host_swm_d64 = os.path.join(host_work_dir, "tune.d64")
    container_swm_d64 = f"{container_work_dir}/tune.d64"
    container_d64 = "/tmp/sidwizard-editor.d64"
    host_vice_state = tempfile.mkdtemp(prefix="sidwizard-driver-ghost-vice-")

    mounts = [
        DiskMount(host_path=args.d64, container_path=container_d64, read_only=True),
        DiskMount(host_path=host_work_dir, container_path=container_work_dir, read_only=False),
        DiskMount(host_path=host_vice_state, container_path=VICE_STATE_DIR, read_only=False),
    ]
    container = ViceContainer(
        image=args.image,
        entrypoint="x64sc",
        binmon_port=args.port,
        autostart=container_d64,
        mounts=mounts,
        warp=True,
    )

    try:
        with container:
            with BinMon(port=args.port) as bm:
                bm.exit()
                sw = Sidwizard(bm)
                tuneheader = sw.wait_for_idle(timeout=args.idle_timeout)
                log.info("TUNEHEADER = $%04X", tuneheader)

                sw.load_swm_via_menu(
                    swm_path=args.swm,
                    host_d64_path=host_swm_d64,
                    container_d64_path=container_swm_d64,
                    tuneheader=tuneheader,
                    load_timeout=args.load_timeout,
                )

                # Pre-arm a stop-on-hit checkpoint at PLAYER_ENTRY before
                # F1 so we halt on the first $1003 dispatch (song frame 0)
                # rather than letting warp run dozens of frames first.
                pre_cp = bm.checkpoint_set(PLAYER_ENTRY, stop_when_hit=True)
                sw.play()

                if args.mute_editor:
                    sw.clear_sid_registers()
                bm.checkpoint_delete(pre_cp.checknum)

                snapshots = _dump_loop(bm, args.frames)

    except ViceContainerError as e:
        print(f"VICE container error: {e}", file=sys.stderr)
        return 4
    except SidwizardError as e:
        print(f"SID-Wizard error: {e}", file=sys.stderr)
        return 5

    rows = _write_csv(snapshots, args.out, annotate=args.annotate)
    print(f"wrote {rows} rows to {args.out} (workdir preserved at {host_work_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
