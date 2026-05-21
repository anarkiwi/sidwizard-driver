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
import time

from vice_driver import BinMon, DiskMount, ViceContainer, ViceContainerError

from .dump import PAL_CYCLES_PER_FRAME, decode_dump_file
from .sidwizard import Sidwizard, SidwizardError

log = logging.getLogger("sidwizard_driver.capture")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--d64", help="SID-Wizard editor .d64 (required unless --dump-only)")
    p.add_argument("--swm", help="SWM module to play (required unless --smoke or --dump-only)")
    p.add_argument("--frames", type=int, default=1500, help="number of PAL frames to capture")
    p.add_argument("--out", required=True, help="output CSV path")
    p.add_argument("--smoke", action="store_true", help="skip SWM load; capture the default editor state")
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
    p.add_argument("--load-settle", type=float, default=2.0,
                   help="wall-clock seconds to wait after picking the SWM in the file dialog")
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p.parse_args(argv)


def _decode_to_csv(dump_path: str, out_path: str, dedup: bool) -> int:
    with open(out_path, "w", newline="") as fp:
        return decode_dump_file(dump_path, fp, dedup=dedup)


def main(argv: list[str] | None = None) -> int:
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


def _run_live(args: argparse.Namespace) -> int:
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

    try:
        with container:
            with BinMon(port=args.port) as bm:
                bm.exit()
                sw = Sidwizard(bm)
                log.info("waiting for SID-Wizard idle...")
                sw.wait_for_idle(timeout=args.idle_timeout)
                tuneheader = sw.discover_tuneheader()
                log.info("TUNEHEADER = $%04X (editor confirmed alive)", tuneheader)

                if args.swm:
                    sw.load_swm_via_menu(
                        swm_path=args.swm,
                        host_d64_path=host_swm_d64,
                        container_d64_path=container_swm_d64,
                        load_settle=args.load_settle,
                    )
                else:
                    log.info("smoke mode: no SWM loaded; capturing default editor state")

                log.info("tapping F1 to play...")
                sw.play()

                # Wait for `frames` PAL frames of player time. Warp
                # mode typically runs at >5x real-time, but conservative
                # to wait at least frames/50 seconds wall-clock so we
                # don't truncate on slower hosts.
                sleep_seconds = max(2.0, args.frames / 50.0)
                log.info("running for ~%.1f wall seconds (>= %d frames in warp)",
                         sleep_seconds, args.frames)
                time.sleep(sleep_seconds)
    except ViceContainerError as e:
        print(f"VICE container error: {e}", file=sys.stderr)
        return 4
    except SidwizardError as e:
        print(f"SID-Wizard error: {e}", file=sys.stderr)
        return 5

    if not os.path.isfile(host_dump):
        print(f"no dump file produced at {host_dump}", file=sys.stderr)
        return 6

    log.info("decoding %s -> %s", host_dump, args.out)
    with open(args.out, "w", newline="") as fp:
        n = decode_dump_file(host_dump, fp, dedup=not args.no_dedup)
    print(f"wrote {n} rows to {args.out} (workdir preserved at {host_work_dir})")
    log.info("PAL cycles/frame = %d; nominal capture window = %d cycles",
             PAL_CYCLES_PER_FRAME, PAL_CYCLES_PER_FRAME * args.frames)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
