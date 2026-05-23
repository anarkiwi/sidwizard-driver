"""sidwizard-driver — Python harness for SID-Wizard inside asid-vice."""

from .d64 import build_d64_with_prg, write_d64_with_prg, write_d64_with_swm
from .fetch import fetch_disk1_d64
from .sidwizard import Sidwizard, SidwizardError

__all__ = [
    "Sidwizard",
    "SidwizardError",
    "build_d64_with_prg",
    "fetch_disk1_d64",
    "write_d64_with_prg",
    "write_d64_with_swm",
]

__version__ = "0.2.0"
