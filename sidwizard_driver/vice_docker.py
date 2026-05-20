"""Manage the asid-vice Docker container that hosts x64sc + binmon.

The image (built from /scratch/anarkiwi/asid-vice via the supplied
Dockerfile) is `asid-vice:latest` by default. Its ENTRYPOINT is `x64sc`
and the default CMD already enables the binary monitor on 0.0.0.0:6502
inside the container, so we only need to publish the port and (optionally)
mount disk images and pass `-autostart`.

This module deliberately uses the docker CLI rather than docker-py so the
harness has no Python dependencies beyond stdlib.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


class ViceContainerError(RuntimeError):
    pass


@dataclass
class DiskMount:
    """A host file made available inside the container."""

    host_path: str
    container_path: str
    read_only: bool = False

    def docker_arg(self) -> list[str]:
        flag = "ro" if self.read_only else "rw"
        return ["-v", f"{os.path.abspath(self.host_path)}:{self.container_path}:{flag}"]


@dataclass
class ViceContainer:
    image: str = "asid-vice:latest"
    binmon_port: int = 6502
    container_binmon_port: int = 6502
    autostart: Optional[str] = None  # container-side path of disk/PRG to autostart
    extra_args: list[str] = field(default_factory=list)
    mounts: list[DiskMount] = field(default_factory=list)
    name: Optional[str] = None  # container name; auto-generated if omitted
    docker_bin: str = "docker"
    container_id: Optional[str] = None
    pull: bool = False  # `docker pull` before run
    warp: bool = True  # -warp (run as fast as possible)
    silent: bool = False  # -silent (suppress VICE startup banner)
    # Stereo SID config. ``sid_extras`` is the number of EXTRA SID chips
    # beyond the always-present SID#1 (VICE's `-sidextra` 0..3); 1 = 2SID.
    # ``sid2_address`` is the base of SID#2 when sid_extras >= 1; defMON's
    # wiki documents $D4xx/$D5xx/$DExx/$DFxx as the four reachable
    # high-byte slots, default $D420.
    sid_extras: int = 0
    sid2_address: int = 0xD420
    sid3_address: int = 0xD440  # used only when sid_extras >= 2
    # VICE true drive emulation on drive 8. Default True matches VICE's
    # ``-default`` behaviour. Set False to pass ``+drive8truedrive`` —
    # disk I/O then uses KERNAL traps instead of cycle-accurate 1541
    # emulation, which is faster and less timing-sensitive. Useful when
    # investigating save-flush regressions that smell like TDE timing.
    truedrive: bool = True
    # Sound device override. Default `dummy` means audio silently discarded.
    # `dump` writes a per-SID-write record to ``sounddump_path`` (container
    # path) — use this when you want a deterministic offline record of
    # what VICE wrote to the SID register space.
    sounddev: str = "dummy"
    sounddump_path: Optional[str] = None

    def __post_init__(self):
        if self.name is None:
            # monotonic_ns is unique per construction even when two
            # ViceContainer() calls land in the same millisecond — which
            # they will if the harness builds containers in a loop.
            self.name = f"asid-vice-{os.getpid()}-{time.monotonic_ns() % 10**9}"

    def x64sc_args(self) -> list[str]:
        """Full command line for x64sc.

        Docker's `CMD` is REPLACED (not appended to) by anything passed
        after the image on the command line, so we must re-specify every
        flag the default CMD provides — binary monitor binding, sound,
        warp — alongside our own additions."""
        args = [
            "-default",
            "-binarymonitor",
            "-binarymonitoraddress",
            f"ip4://0.0.0.0:{self.container_binmon_port}",
            "-sounddev",
            self.sounddev,
        ]
        if self.sounddev == "dump" and self.sounddump_path is not None:
            args += ["-soundarg", self.sounddump_path]
        if self.warp:
            args.append("-warp")
        if self.silent:
            args.append("-silent")
        if not self.truedrive:
            args.append("+drive8truedrive")
        if self.sid_extras > 0:
            # VICE: -sidextra is the count of EXTRA chips (0..3), -sidNaddress
            # gives each extra chip its base address (high byte must be one
            # of $D4/$D5/$DE/$DF for defMON to recognise it).
            args += [
                "-sidextra",
                str(self.sid_extras),
                "-sid2address",
                f"0x{self.sid2_address:04x}",
            ]
            if self.sid_extras >= 2:
                args += ["-sid3address", f"0x{self.sid3_address:04x}"]
        if self.autostart is not None:
            args += ["-autostart", self.autostart]
        if self.extra_args:
            args += list(self.extra_args)
        return args

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self.container_id is not None:
            raise ViceContainerError("already started")
        if shutil.which(self.docker_bin) is None:
            raise ViceContainerError(f"docker binary {self.docker_bin!r} not on PATH")

        if self.pull:
            subprocess.run([self.docker_bin, "pull", self.image], check=True)

        cmd: list[str] = [
            self.docker_bin,
            "run",
            "-d",
            "--rm",
            "--name",
            self.name,  # type: ignore[list-item]
            "-p",
            f"{self.binmon_port}:{self.container_binmon_port}",
        ]
        for m in self.mounts:
            cmd += m.docker_arg()
        cmd += [self.image]
        cmd += self.x64sc_args()

        log.info("starting container: %s", " ".join(cmd))
        try:
            cid = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()
        except subprocess.CalledProcessError as e:
            raise ViceContainerError(f"docker run failed: {e.stderr.strip() or e.stdout}") from e
        self.container_id = cid
        log.info("container id: %s", cid)

    def stop(self, timeout: int = 5) -> None:
        if self.container_id is None:
            return
        try:
            subprocess.run(
                [self.docker_bin, "stop", "-t", str(timeout), self.container_id],
                check=False,
                capture_output=True,
            )
        finally:
            self.container_id = None

    def is_running(self) -> bool:
        if self.container_id is None:
            return False
        r = subprocess.run(
            [self.docker_bin, "inspect", "-f", "{{.State.Running}}", self.container_id],
            capture_output=True,
            text=True,
        )
        return r.returncode == 0 and r.stdout.strip() == "true"

    def logs(self) -> str:
        if self.container_id is None:
            return ""
        r = subprocess.run(
            [self.docker_bin, "logs", self.container_id],
            capture_output=True,
            text=True,
        )
        return (r.stdout or "") + (r.stderr or "")

    def __enter__(self) -> "ViceContainer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
