"""Capture SID register writes from SID-Wizard's real player.

Run: ``python -m sidwizard_driver.capture --d64 disk1.d64 --swm tune.swm
                                          --frames 1500 --out tune.csv``

Boots SID-Wizard inside asid-vice with ``-sounddev dump``, loads
``tune.swm`` via SID-Wizard's own disk menu (so the in-place depack
runs), taps F1 to play, waits long enough for ``--frames`` PAL frames
of CPU work, then decodes the dump file into a ``(frame, reg, value)``
CSV matching pysidwizard's render_wav sibling schema.

The SWM is delivered to the editor by building a fresh single-file
``.d64`` on the host (see :mod:`sidwizard_driver.d64`), mounting the
host tempdir read-write into the container, and using asid-vice's
``DRIVE_ATTACH`` (binmon opcode 0x78) to swap drive 8 to the new disk
once the editor is idle. Then keymatrix taps drive
SHIFT+F7 → CRSRDOWN → RETURN → RETURN to enter loadtun and pick the
first directory entry.

Useful operating modes
----------------------
* default: full live capture against ``--d64`` and ``--swm``.
* ``--smoke`` : skip the SWM load; capture whatever the freshly-booted
  editor emits. Useful for verifying the dump → CSV pipeline.
* ``--dump-only PATH`` : skip VICE entirely and re-decode a previously
  captured dump file. Useful while iterating on the CSV schema.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile

from vice_driver import BinMon, DiskMount, ViceContainer, ViceContainerError

from .dump import PAL_CYCLES_PER_FRAME, decode_dump_file
from .sidwizard import Sidwizard, SidwizardError

# SWM header: 2-byte PRG load address, then 4 bytes magic "SWM1",
# then byte at offset 4 = framespeed. Mirrors pysidwizard.constants
# (FRAMESPEED_POS = 0x04).
SWM_FRAMESPEED_OFFSET = 2 + 0x04


def _read_swm_framespeed(swm_path: str) -> int:
    """Return the framespeed byte from a ``.swm`` file (1 for normal
    play; 2 for double-speed tunes like euphoria.swm). The player runs
    its update routine ``framespeed`` times per video frame, so
    ``cycles_per_frame = 19656 // framespeed`` is the right granularity
    for the captured CSV — matches
    :data:`pysidwizard.player.SWMPlayer.cycles_per_frame`.
    """
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
    p.add_argument("--d64", help="SID-Wizard editor .d64 (required unless --dump-only)")
    p.add_argument("--swm", help="SWM module to play (required unless --smoke or --dump-only)")
    p.add_argument("--frames", type=int, default=1500, help="number of PAL frames to capture")
    p.add_argument("--out", required=True, help="output CSV path")
    p.add_argument(
        "--smoke",
        action="store_true",
        help="skip SWM load; capture the default editor state",
    )
    p.add_argument(
        "--dump-only",
        metavar="PATH",
        help="re-decode an existing dump file instead of running VICE",
    )
    p.add_argument(
        "--no-dedup",
        action="store_true",
        help="don't collapse consecutive duplicate writes per register",
    )
    p.add_argument("--image", default="asid-vice:latest")
    p.add_argument("--port", type=int, default=6502)
    p.add_argument("--idle-timeout", type=float, default=60.0)
    p.add_argument(
        "--load-timeout",
        type=float,
        default=10.0,
        help="seconds to wait for TUNEHEADER author byte to change after the RETURN that triggers loadtun",
    )
    p.add_argument(
        "--capture-timeout",
        type=float,
        default=120.0,
        help="wall-clock cap on the cycle-counter wait for --frames frames",
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

    if not args.d64:
        print("--d64 is required (or use --dump-only)", file=sys.stderr)
        return 2
    if not args.smoke and not args.swm:
        print("--swm is required (or pass --smoke)", file=sys.stderr)
        return 2

    return _run_live(args)


def _run_live(args: argparse.Namespace) -> int:  # pragma: no cover - requires live VICE
    if not os.path.isfile(args.d64):
        print(f"not a file: {args.d64}", file=sys.stderr)
        return 2
    if args.swm and not os.path.isfile(args.swm):
        print(f"not a file: {args.swm}", file=sys.stderr)
        return 2

    # One host tempdir houses both the SWM .d64 (written at runtime)
    # and the sounddev=dump trace. Mounted read-write into the
    # container so attach_drive can swap to it and the dump driver
    # can write into it.
    host_work_dir = tempfile.mkdtemp(prefix="sidwizard-driver-")
    container_work_dir = "/tmp/sidwizard-driver"
    host_dump = os.path.join(host_work_dir, "trace.txt")
    container_dump = f"{container_work_dir}/trace.txt"
    host_swm_d64 = os.path.join(host_work_dir, "tune.d64")
    container_swm_d64 = f"{container_work_dir}/tune.d64"

    container_d64 = "/tmp/sidwizard-editor.d64"

    mounts = [
        DiskMount(host_path=args.d64, container_path=container_d64, read_only=True),
        DiskMount(host_path=host_work_dir, container_path=container_work_dir, read_only=False),
    ]
    container = ViceContainer(
        image=args.image,
        binmon_port=args.port,
        autostart=container_d64,
        mounts=mounts,
        warp=True,
        silent=True,
        sounddev="dump",
        sounddump_path=container_dump,
    )

    # Compute cycles-per-frame up-front from the SWM's framespeed (1
    # for normal tunes; 2 for euphoria). ``--frames N`` always means
    # "N player frames" so the cycle budget scales with framespeed.
    if args.swm:
        framespeed = _read_swm_framespeed(args.swm)
    else:
        framespeed = 1
    cycles_per_frame = PAL_CYCLES_PER_FRAME // framespeed
    target_cycles = args.frames * cycles_per_frame

    start_cycle = 0
    try:
        with container:
            with BinMon(port=args.port) as bm:
                bm.exit()
                sw = Sidwizard(bm)
                log.info("waiting for SID-Wizard idle...")
                tuneheader = sw.wait_for_idle(timeout=args.idle_timeout)
                log.info("TUNEHEADER = $%04X (editor confirmed alive)", tuneheader)

                if args.swm:
                    sw.load_swm_via_menu(
                        swm_path=args.swm,
                        host_d64_path=host_swm_d64,
                        container_d64_path=container_swm_d64,
                        tuneheader=tuneheader,
                        load_timeout=args.load_timeout,
                    )
                else:
                    log.info("smoke mode: no SWM loaded; capturing default editor state")

                log.info("tapping F1 to play...")
                sw.play()

                # Poll the CPU cycle counter rather than wall-clock
                # sleeping: warp mode is much faster than real-time, so
                # cycle polling finishes as soon as the player has
                # actually run for `frames` player frames. The
                # start_cycle gives us the anchor for frame-zero
                # alignment in the CSV downstream.
                log.info(
                    "waiting for %d cycles (%d player frames @ framespeed=%d) since F1...",
                    target_cycles,
                    args.frames,
                    framespeed,
                )
                start_cycle, end_cycle = sw.wait_for_cycles(
                    target_cycles, timeout=args.capture_timeout
                )
                log.info(
                    "captured %d cycles (start=%d, end=%d)",
                    end_cycle - start_cycle,
                    start_cycle,
                    end_cycle,
                )
    except ViceContainerError as e:
        print(f"VICE container error: {e}", file=sys.stderr)
        return 4
    except SidwizardError as e:
        print(f"SID-Wizard error: {e}", file=sys.stderr)
        return 5

    if not os.path.isfile(host_dump):
        print(f"no dump file produced at {host_dump}", file=sys.stderr)
        return 6

    log.info(
        "decoding %s -> %s (frame 0 anchored at cycle %d; "
        "framespeed=%d, cycles/frame=%d; cap = %d frames)",
        host_dump,
        args.out,
        start_cycle,
        framespeed,
        cycles_per_frame,
        args.frames,
    )
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
    log.info("nominal capture window = %d cycles", target_cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
