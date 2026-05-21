"""sidwizard-driver — Python harness for SID-Wizard inside asid-vice.

What this models
----------------
A thin automation layer that boots SID-Wizard on the asid-vice C64 emulator,
drives the editor's own disk-menu loader to attach a ``.swm`` module,
presses F1, and captures every SID register write the real 6502 player
emits. The product is a deduplicated ``(frame, reg, value)`` CSV — ground
truth for the from-scratch Python player in :mod:`pysidwizard.player`.

This package depends on the upstream ``vice-driver`` package for the
editor-agnostic asid-vice client (``BinMon``, ``ViceContainer``, the
key matrix, ``ScreenSnapshot``). Everything in ``sidwizard_driver`` is
SID-Wizard- or SWM-specific.

Scope (v0)
----------
* :mod:`sidwizard_driver.sidwizard` — Sidwizard class:
  ``wait_for_startup_menu`` / ``dismiss_startup_menu`` /
  ``wait_for_editor`` (the editor doesn't hook ``$0314/$0315`` so the
  "editor alive" signal is the loadtun-signature scan returning a
  ``SWM1``-pointing address); ``load_swm_via_menu``; ``play``.
* :mod:`sidwizard_driver.d64` — pure-Python single-file ``.d64`` writer.
* :mod:`sidwizard_driver.dump` — decoder for VICE ``sounddev=dump`` files.
* :mod:`sidwizard_driver.capture` — end-to-end CLI.

Not modelled
------------
* Editor automation beyond loading + playing (field setters, save flow,
  pattern editing).
* Multi-SID, ``.sws`` stereo modules, and ``frame_speed > 1`` semantics
  beyond a basic constant — see ``AGENTS.md`` "Open questions".
"""

from .d64 import build_d64_with_prg, write_d64_with_prg, write_d64_with_swm
from .sidwizard import Sidwizard, SidwizardError

__all__ = [
    "Sidwizard",
    "SidwizardError",
    "build_d64_with_prg",
    "write_d64_with_prg",
    "write_d64_with_swm",
]

__version__ = "0.2.0"
