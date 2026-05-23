"""Skip-guard for integration tests that need Docker + anarkiwi/headlessvice."""

from __future__ import annotations

import shutil
import subprocess

import pytest


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return True


@pytest.fixture(scope="session")
def docker_required() -> None:
    if not _docker_available():
        pytest.skip("docker not available")
