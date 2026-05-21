"""sidwizard-driver — Python harness for SID-Wizard inside asid-vice.

What this models
----------------
A thin automation layer that boots SID-Wizard on the asid-vice C64 emulator,
side-loads a ``.swm`` module, presses F1, and captures every SID register
write the real 6502 player emits. The product is a deduplicated
``(frame, reg, value)`` CSV — ground truth for the from-scratch Python
player in :mod:`pysidwizard.player`.

Scope (v0)
----------
* Vendored binmon / vice_docker / keys layers from defmon-driver (the
  protocol they speak is editor-agnostic).
* :mod:`sidwizard_driver.sidwizard` — minimal Sidwizard class:
  ``wait_for_idle``, ``side_load_swm``, ``play``.
* :mod:`sidwizard_driver.dump` — decoder for VICE ``sounddev=dump`` files.
* :mod:`sidwizard_driver.capture` — end-to-end CLI.

Not modelled
------------
* Disk-menu navigation (F7 / filename typing) — the harness side-loads
  modules straight to ``$1FF8`` instead.
* Editor automation (field setters, save flow, screen scraping).
* Multi-SID, ``.sws`` stereo modules, and ``frame_speed > 1`` semantics
  beyond a basic constant — see ``AGENTS.md`` "Open questions".
"""

from .binmon import OPCODE, BinMon, BinmonError
from .d64 import build_d64_with_prg, write_d64_with_prg, write_d64_with_swm
from .keys import KEY
from .screen import ScreenSnapshot, parse_screen_response, screencode_to_ascii
from .sidwizard import Sidwizard, SidwizardError
from .vice_docker import DiskMount, ViceContainer, ViceContainerError

__all__ = [
    "BinMon",
    "BinmonError",
    "OPCODE",
    "KEY",
    "ScreenSnapshot",
    "parse_screen_response",
    "screencode_to_ascii",
    "Sidwizard",
    "SidwizardError",
    "ViceContainer",
    "ViceContainerError",
    "DiskMount",
    "build_d64_with_prg",
    "write_d64_with_prg",
    "write_d64_with_swm",
]

__version__ = "0.1.0"
