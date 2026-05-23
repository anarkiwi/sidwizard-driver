"""Live integration smoke: boot SID-Wizard inside anarkiwi/headlessvice
and confirm the editor comes up with a discoverable TUNEHEADER.

Opt-in: ``pytest -m integration``. Requires Docker and the
``anarkiwi/headlessvice:latest`` image (pre-pulled or reachable on the
daemon). The SID-Wizard editor disk is fetched on demand and cached
under ``$XDG_CACHE_HOME/sidwizard-driver/``.
"""

from __future__ import annotations

import os

import pytest

from sidwizard_driver.fetch import fetch_disk1_d64
from sidwizard_driver.smoke import main as smoke_main

IMAGE = os.environ.get("SIDWIZARD_VICE_IMAGE", "anarkiwi/headlessvice:latest")
PORT = int(os.environ.get("SIDWIZARD_BINMON_PORT", "6502"))


@pytest.mark.integration
def test_smoke_boots_editor_and_discovers_tuneheader(docker_required, capsys):
    """End-to-end boot: container starts, bootloader → editor, TUNEHEADER
    discoverable. The signal is ``smoke.main`` returning 0 and emitting a
    ``TUNEHEADER = $XXXX`` line — any other outcome (timeout, container
    error, ambiguous signature) is a fail."""
    d64 = fetch_disk1_d64()
    rc = smoke_main(
        [
            "--d64",
            str(d64),
            "--image",
            IMAGE,
            "--port",
            str(PORT),
            "--idle-timeout",
            "120.0",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "TUNEHEADER = $" in captured.out, captured.out
