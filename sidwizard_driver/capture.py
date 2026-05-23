"""Capture SID register writes from SID-Wizard's real player to CSV."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile

from vice_driver import BinMon, DiskMount, ViceContainer, ViceContainerError

from .dump import PAL_CYCLES_PER_FRAME, decode_dump_file
from .fetch import fetch_disk1_d64
from .sidwizard import Sidwizard, SidwizardError

# SWM header: 2-byte PRG load address + 4-byte magic + framespeed byte at +4.
SWM_FRAMESPEED_OFFSET = 2 + 0x04

# anarkiwi/headlessvice's x64sc needs a writable $HOME/.local/state/vice.
VICE_STATE_DIR = "/root/.local/state/vice"


def _read_swm_framespeed(swm_path: str) -> int:
    with open(swm_path, "rb") as fp:
        head = fp.read(SWM_FRAMESPEED_OFFSET + 1)
    if len(head) <= SWM_FRAMESPEED_OFFSET:
        raise ValueError(f"{swm_path}: too short to read framespeed")
    fs = head[SWM_FRAMESPEED_OFFSET]
    if not 1 <= fs <= 4:
        raise ValueError(f"{swm_path}: unreasonable framespeed value {fs}")
    return fs


log = logging.getLogger("sidwizard_driver.capture")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:  # pragma: no cover - CLI glue
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--d64", help="SID-Wizard editor .d64 (auto-fetched if omitted)")
    p.add_argument("--swm", help="SWM module to play (required unless --smoke or --dump-only)")
    p.add_argument("--frames", type=int, default=1500, help="number of PAL frames to capture")
    p.add_argument("--out", required=True, help="output CSV path")
    p.add_argument(
        "--smoke", action="store_true", help="skip SWM load; capture default editor state"
    )
    p.add_argument("--dump-only", metavar="PATH", help="re-decode an existing dump file")
    p.add_argument(
        "--no-dedup", action="store_true", help="don't collapse consecutive duplicate writes"
    )
    p.add_argument("--image", default="anarkiwi/headlessvice:latest")
    p.add_argument("--port", type=int, default=6502)
    p.add_argument("--idle-timeout", type=float, default=60.0)
    p.add_argument("--load-timeout", type=float, default=10.0)
    p.add_argument("--capture-timeout", type=float, default=120.0)
    p.add_argument(
        "--mute-editor",
        action="store_true",
        help="zero $D400-$D418 at the pre-F1 checkpoint to suppress editor audition residue",
    )
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p.parse_args(argv)


def _decode_to_csv(
    dump_path: str, out_path: str, dedup: bool
) -> int:  # pragma: no cover - CLI glue
    with open(out_path, "w", newline="") as fp:
        return decode_dump_file(dump_path, fp, dedup=dedup)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI entry point
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.dump_only:
        if not os.path.isfile(args.dump_only):
            print(f"not a file: {args.dump_only}", file=sys.stderr)
            return 2
        n = _decode_to_csv(args.dump_only, args.out, dedup=not args.no_dedup)
        print(f"wrote {n} rows to {args.out}")
        return 0

    if not args.smoke and not args.swm:
        print("--swm is required (or pass --smoke)", file=sys.stderr)
        return 2

    if not args.d64:
        args.d64 = str(fetch_disk1_d64())

    return _run_live(args)


def _run_live(args: argparse.Namespace) -> int:  # pragma: no cover - requires live VICE
    if not os.path.isfile(args.d64):
        print(f"not a file: {args.d64}", file=sys.stderr)
        return 2
    if args.swm and not os.path.isfile(args.swm):
        print(f"not a file: {args.swm}", file=sys.stderr)
        return 2

    host_work_dir = tempfile.mkdtemp(prefix="sidwizard-driver-")
    container_work_dir = "/tmp/sidwizard-driver"
    host_dump = os.path.join(host_work_dir, "trace.txt")
    container_dump = f"{container_work_dir}/trace.txt"
    host_swm_d64 = os.path.join(host_work_dir, "tune.d64")
    container_swm_d64 = f"{container_work_dir}/tune.d64"
    host_vice_state = tempfile.mkdtemp(prefix="sidwizard-driver-vice-")

    container_d64 = "/tmp/sidwizard-editor.d64"

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
        sounddev="dump",
        sounddump_path=container_dump,
    )

    framespeed = _read_swm_framespeed(args.swm) if args.swm else 1
    cycles_per_frame = PAL_CYCLES_PER_FRAME // framespeed
    target_cycles = args.frames * cycles_per_frame

    start_cycle = 0
    try:
        with container:
            with BinMon(port=args.port) as bm:
                bm.exit()
                sw = Sidwizard(bm)
                tuneheader = sw.wait_for_idle(timeout=args.idle_timeout)
                log.info("TUNEHEADER = $%04X", tuneheader)

                if args.swm:
                    sw.load_swm_via_menu(
                        swm_path=args.swm,
                        host_d64_path=host_swm_d64,
                        container_d64_path=container_swm_d64,
                        tuneheader=tuneheader,
                        load_timeout=args.load_timeout,
                    )

                # Pre-arm a stop-on-hit checkpoint at $1003 (PLAYER
                # dispatch) BEFORE F1, so frame 0 anchors to the first
                # player tick instead of to the keypress (~0.5s of menu
                # IRQ state).
                pre_cp = bm.checkpoint_set(0x1003, stop_when_hit=True)
                sw.play()

                start_cycle = sw.cycle()
                if args.mute_editor:
                    sw.clear_sid_registers()
                bm.checkpoint_delete(pre_cp.checknum)
                _, end_cycle = sw.wait_for_cycles(target_cycles, timeout=args.capture_timeout)
                log.info("captured %d cycles", end_cycle - start_cycle)
    except ViceContainerError as e:
        print(f"VICE container error: {e}", file=sys.stderr)
        return 4
    except SidwizardError as e:
        print(f"SID-Wizard error: {e}", file=sys.stderr)
        return 5

    if not os.path.isfile(host_dump):
        print(f"no dump file produced at {host_dump}", file=sys.stderr)
        return 6

    with open(args.out, "w", newline="") as fp:
        n = decode_dump_file(
            host_dump,
            fp,
            cycles_per_frame=cycles_per_frame,
            dedup=not args.no_dedup,
            start_cycle=start_cycle,
            max_frame=args.frames - 1,
        )
    print(f"wrote {n} rows to {args.out} (workdir preserved at {host_work_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
