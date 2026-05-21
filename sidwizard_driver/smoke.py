"""End-to-end boot smoke for SID-Wizard inside asid-vice.

Run: ``python -m sidwizard_driver.smoke <path/to/SID-Wizard-1.94-disk1.d64>``

Boots the supplied ``.d64``, waits for SID-Wizard to hook the IRQ
vector, attempts to discover ``TUNEHEADER`` from the loadtun byte
signature, and prints both facts. Exit code 0 on success.

This is the half-day Phase 0 check from AGENTS.md — it does NOT load
an ``.swm`` or capture SID writes; for that, see
:mod:`sidwizard_driver.capture`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .binmon import BinMon
from .sidwizard import Sidwizard, SidwizardError
from .vice_docker import DiskMount, ViceContainer, ViceContainerError

log = logging.getLogger("sidwizard_driver.smoke")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("d64", help="host path to SID-Wizard-1.94-disk1.d64")
    parser.add_argument("--image", default="asid-vice:latest", help="docker image")
    parser.add_argument(
        "--port",
        type=int,
        default=6502,
        help="host binmon port to publish",
    )
    parser.add_argument("--no-warp", action="store_true", help="disable VICE warp")
    parser.add_argument("--idle-timeout", type=float, default=60.0)
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if not os.path.isfile(args.d64):
        print(f"not a file: {args.d64}", file=sys.stderr)
        return 2

    container_d64 = "/tmp/sidwizard.d64"
    mount = DiskMount(host_path=args.d64, container_path=container_d64, read_only=True)
    container = ViceContainer(
        image=args.image,
        binmon_port=args.port,
        autostart=container_d64,
        mounts=[mount],
        warp=not args.no_warp,
        silent=True,
    )

    try:
        with container:
            with BinMon(port=args.port) as bm:
                # Acknowledge the initial STOPPED so the CPU starts running
                # the autostart sequence.
                bm.exit()
                sw = Sidwizard(bm)
                log.info("driving bootloader → startup menu → editor...")
                tuneheader = sw.wait_for_idle(timeout=args.idle_timeout)
                print(f"SID-Wizard booted; TUNEHEADER = ${tuneheader:04X}")
                return 0
    except ViceContainerError as e:
        print(f"VICE container error: {e}", file=sys.stderr)
        return 4
    except SidwizardError as e:
        print(f"SID-Wizard error: {e}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
