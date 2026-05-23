"""Boot smoke for SID-Wizard inside asid-vice.

Boots the editor d64, drives the bootloader through to the live tracker,
discovers TUNEHEADER, prints it. Does not load an SWM or capture writes.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from vice_driver import BinMon, DiskMount, ViceContainer, ViceContainerError

from .fetch import fetch_disk1_d64
from .sidwizard import Sidwizard, SidwizardError

log = logging.getLogger("sidwizard_driver.smoke")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--d64", default=None, help="SID-Wizard editor .d64 (auto-fetched if omitted)"
    )
    parser.add_argument("--image", default="asid-vice:latest")
    parser.add_argument("--port", type=int, default=6502)
    parser.add_argument("--no-warp", action="store_true")
    parser.add_argument("--idle-timeout", type=float, default=60.0)
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    d64_path = args.d64 or str(fetch_disk1_d64())
    if not os.path.isfile(d64_path):
        print(f"not a file: {d64_path}", file=sys.stderr)
        return 2

    container_d64 = "/tmp/sidwizard.d64"
    mount = DiskMount(host_path=d64_path, container_path=container_d64, read_only=True)
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
                bm.exit()
                sw = Sidwizard(bm)
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
