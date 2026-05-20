"""Capture SID register writes from SID-Wizard's real player.

Run: ``python -m sidwizard_driver.capture --d64 disk1.d64 --swm tune.swm
                                          --frames 1500 --out tune.csv``

Boots SID-Wizard inside asid-vice with ``-sounddev dump``, side-loads
``tune.swm`` into ``TUNEHEADER``, taps F1 to play, waits long enough
for ``--frames`` PAL frames of CPU work, then decodes the dump file
into a ``(frame, reg, value)`` CSV matching pysidwizard's render_wav
sibling schema.

Status (v0)
-----------
End-to-end execution is **gated on the side-load gap** documented in
``AGENTS.md``: ``side_load_swm`` writes the packed SWM payload to
TUNEHEADER but does NOT call SID-Wizard's in-place depacker
(``depackt``) or post-load init (``dispaut`` / subtune reset). With
the gap, ``--frames`` worth of output is still produced (you get the
register writes the player does over whatever stale data lives at
TUNEHEADER), but it is not yet the ground truth needed to diff
pysidwizard.

Until the gap closes, the useful operating modes are:

  * ``--smoke``  : run the full pipeline against a freshly-booted
    editor (no SWM loaded; you'll capture whatever the default empty
    tune sounds like) — useful for verifying the dump → CSV pipeline.
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
from typing import Optional

from .binmon import BinMon
from .dump import PAL_CYCLES_PER_FRAME, decode_dump_file
from .sidwizard import Sidwizard, SidwizardError
from .vice_docker import DiskMount, ViceContainer, ViceContainerError

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

    # ViceContainer writes the dump file to a container-side path; mount
    # a host tempdir read-write so we can read it back after stop().
    host_dump_dir = tempfile.mkdtemp(prefix="sidwizard-dump-")
    container_dump_dir = "/tmp/sidwizard-dump"
    host_dump = os.path.join(host_dump_dir, "trace.txt")
    container_dump = f"{container_dump_dir}/trace.txt"

    container_d64 = "/tmp/sidwizard.d64"

    mounts = [
        DiskMount(host_path=args.d64, container_path=container_d64, read_only=True),
        DiskMount(host_path=host_dump_dir, container_path=container_dump_dir, read_only=False),
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
            tuneheader: Optional[int] = None
            with BinMon(port=args.port) as bm:
                bm.exit()
                sw = Sidwizard(bm)
                log.info("waiting for SID-Wizard idle...")
                sw.wait_for_idle(timeout=args.idle_timeout)
                tuneheader = sw.discover_tuneheader()
                log.info("TUNEHEADER = $%04X", tuneheader)

                if args.swm:
                    log.warning(
                        "side-loading %s — note: in-place depack / post-load init"
                        " is NOT yet implemented; the captured trace is not yet"
                        " comparable to pysidwizard's player. See AGENTS.md.",
                        args.swm,
                    )
                    sw.side_load_swm(args.swm, tuneheader)
                else:
                    log.info("smoke mode: no SWM loaded; capturing default editor state")

                log.info("tapping F1 to play...")
                sw.play()

                # Wait for `frames` PAL frames of player time. Warp mode
                # runs much faster than real-time, but the safe bound is
                # to wait wall-clock for at least frames/50 seconds and
                # also for the SID write rate to settle. Simple first
                # implementation: wall-clock sleep on a generous bound.
                sleep_seconds = max(2.0, args.frames / 50.0 / 5.0)
                log.info("running for ~%.1f wall seconds (%d frames @ ~5x realtime warp)",
                         sleep_seconds, args.frames)
                time.sleep(sleep_seconds)
    except ViceContainerError as e:
        print(f"VICE container error: {e}", file=sys.stderr)
        return 4
    except SidwizardError as e:
        print(f"SID-Wizard error: {e}", file=sys.stderr)
        return 5

    # Decode the captured dump file.
    if not os.path.isfile(host_dump):
        print(f"no dump file produced at {host_dump}", file=sys.stderr)
        return 6

    # Trim to --frames worth of output by setting start_cycle to 0 (we
    # don't know exactly when F1 fired in CPU cycles) and discarding
    # rows past PAL_CYCLES_PER_FRAME * frames. Imperfect — see TODOs.
    log.info("decoding %s -> %s", host_dump, args.out)
    with open(args.out, "w", newline="") as fp:
        n = decode_dump_file(host_dump, fp, dedup=not args.no_dedup)
    print(f"wrote {n} rows to {args.out} (dump preserved at {host_dump})")
    log.info("PAL cycles/frame = %d; nominal capture window = %d cycles",
             PAL_CYCLES_PER_FRAME, PAL_CYCLES_PER_FRAME * args.frames)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
