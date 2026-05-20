"""Minimal SID-Wizard automation wrapper.

What this models
----------------
A small high-level layer on top of :class:`sidwizard_driver.binmon.BinMon`
exposing the actions the capture loop needs:

* :meth:`Sidwizard.wait_for_idle` — block until SID-Wizard has booted
  from disk1.d64 and installed its IRQ handler.
* :meth:`Sidwizard.discover_tuneheader` — locate SID-Wizard's
  ``TUNEHEADER`` symbol in the running editor by pattern-matching the
  ``loadtun`` byte sequence in RAM.
* :meth:`Sidwizard.side_load_swm` — write a ``.swm`` module's payload
  into ``TUNEHEADER`` so the next play command picks it up.
* :meth:`Sidwizard.play` — F1 (reset + play whole tune from beginning).

Scope (v0)
----------
* PAL-only, single-SID.
* No disk-menu automation. The side-load path bypasses ``KERNAL.LOAD``.

Not modelled / open
-------------------
* **In-place depack after side-load.** The on-disk ``.swm`` is packed;
  SID-Wizard's normal ``loadtun`` calls ``depackt`` after ``KERNAL.LOAD``
  to expand it in place at ``TUNEHEADER``. A complete side-load needs
  to either (a) call ``depackt`` after the ``mem_set``, or (b) write the
  already-unpacked form. Neither is implemented yet; see
  ``AGENTS.md`` "Side-load gap" for the plan.
* **Disk-menu path.** Listed as a future workstream if side-load proves
  too brittle.
"""

from __future__ import annotations

import logging
import struct
import time
from typing import Optional

from .binmon import BinMon
from .keys import KEY

log = logging.getLogger(__name__)


# C64 default IRQ vector at $0314/$0315 points at $EA31 in the KERNAL.
# SID-Wizard rewrites this vector to its own player entry once boot is
# complete, so polling these two bytes is the cheapest "is the editor
# alive yet?" signal we have without a known editor-internal address.
IRQ_VECTOR_LO = 0x0314
IRQ_VECTOR_HI = 0x0315
KERNAL_DEFAULT_IRQ = 0xEA31

# Editor RAM scan range. SID-Wizard's editor PRG loads from $0801
# (BASIC stub + SYS) and grows up; capping at $C000 stays below the
# KERNAL/IO/BASIC ROM area.
EDITOR_SCAN_LO = 0x0800
EDITOR_SCAN_HI = 0xC000

# Loadtun signature: the assembled byte sequence emitted by
#   ldx #<TUNEHEADER ; A2 lo
#   ldy #>TUNEHEADER ; A0 hi
#   lda #$00         ; A9 00
#   jsr KERNAL.LOAD  ; 20 D5 FF
# (see /tmp/sidwizard/SID-Wizard-1.94/native/sources/include/menu.inc
#  around the ``loadtun`` label). Bytes 1 and 3 (TUNEHEADER lo/hi)
# are the immediate operands we want to recover.
LOADTUN_TAIL = bytes([0xA9, 0x00, 0x20, 0xD5, 0xFF])
LOADTUN_LDX = 0xA2
LOADTUN_LDY = 0xA0


class SidwizardError(RuntimeError):
    pass


class Sidwizard:
    """High-level driver for a running SID-Wizard inside asid-vice."""

    def __init__(self, bm: BinMon):
        self.bm = bm

    # ---- boot --------------------------------------------------------

    def wait_for_idle(self, timeout: float = 60.0, poll_interval: float = 0.2) -> None:
        """Block until SID-Wizard has hooked the IRQ vector.

        Polls ``$0314/$0315`` until it stops pointing at the KERNAL
        default ($EA31) AND has held the same value for two consecutive
        polls (so we don't trip on a transient mid-boot setup write).
        """
        deadline = time.monotonic() + timeout
        last_vec: Optional[int] = None
        stable_polls = 0
        while time.monotonic() < deadline:
            data = self.bm.mem_get(IRQ_VECTOR_LO, IRQ_VECTOR_HI)
            if len(data) >= 2:
                vec = data[0] | (data[1] << 8)
                if vec != KERNAL_DEFAULT_IRQ:
                    if vec == last_vec:
                        stable_polls += 1
                        if stable_polls >= 2:
                            log.info("SID-Wizard IRQ hooked at $%04X", vec)
                            return
                    else:
                        stable_polls = 1
                    last_vec = vec
                else:
                    stable_polls = 0
                    last_vec = None
            time.sleep(poll_interval)
        raise SidwizardError(
            f"timed out after {timeout:.0f}s waiting for SID-Wizard to hook IRQ"
            f" (last vector seen: {last_vec})"
        )

    # ---- TUNEHEADER discovery ---------------------------------------

    def discover_tuneheader(self) -> int:
        """Find the address of SID-Wizard's ``TUNEHEADER`` symbol by
        scanning editor RAM for the assembled ``loadtun`` byte sequence.

        Raises :class:`SidwizardError` if zero or multiple candidate
        matches are found — the caller should investigate before
        proceeding because a wrong TUNEHEADER will corrupt the editor.
        """
        ram = self.bm.mem_get(EDITOR_SCAN_LO, EDITOR_SCAN_HI - 1)
        candidates: list[int] = []
        # Window: [LDX_op imm_lo LDY_op imm_hi tail...]. Length 4 + len(tail).
        for i in range(len(ram) - (4 + len(LOADTUN_TAIL)) + 1):
            if (
                ram[i] == LOADTUN_LDX
                and ram[i + 2] == LOADTUN_LDY
                and ram[i + 4 : i + 4 + len(LOADTUN_TAIL)] == LOADTUN_TAIL
            ):
                lo = ram[i + 1]
                hi = ram[i + 3]
                addr = lo | (hi << 8)
                candidates.append(addr)
        if not candidates:
            raise SidwizardError(
                "loadtun signature not found in editor RAM "
                f"({EDITOR_SCAN_LO:#06x}-{EDITOR_SCAN_HI:#06x}); "
                "editor may not have finished loading"
            )
        # Multiple unique candidates would mean the signature is not
        # unique enough; SID-Wizard's loadtun is the only such sequence
        # in a stock build, but warn loudly if that changes.
        unique = sorted(set(candidates))
        if len(unique) > 1:
            raise SidwizardError(
                f"loadtun signature is ambiguous; candidates: "
                f"{', '.join(f'${a:04X}' for a in unique)}"
            )
        return unique[0]

    # ---- side load --------------------------------------------------

    def side_load_swm(self, swm_path: str, tuneheader: int) -> int:
        """Write the packed ``.swm`` payload to ``tuneheader``.

        Strips the standard 2-byte PRG header (``$1FF8`` little-endian
        load address — see :data:`pysidwizard.constants.DEFAULT_LOAD_ADDRESS`).
        Returns the address one past the last loaded byte, which
        SID-Wizard's loader writes to ``compzptr`` for the in-place
        depacker. The caller is responsible for arranging the depack /
        init step (see "Open question" in the module docstring) before
        :meth:`play`.
        """
        with open(swm_path, "rb") as fp:
            blob = fp.read()
        if len(blob) < 2:
            raise SidwizardError(f"{swm_path}: too short to be a PRG")
        # First two bytes are the PRG load address; we don't need them
        # because we choose the target ourselves. Sanity-check that
        # they match SID-Wizard's documented default — a non-matching
        # value is not fatal but probably indicates a wrong file.
        load_addr = struct.unpack("<H", blob[:2])[0]
        if load_addr != 0x1FF8:
            log.warning(
                "%s: PRG load address $%04X != $1FF8 (DEFAULT_LOAD_ADDRESS);"
                " probably still ok, the loader forces TUNEHEADER",
                swm_path,
                load_addr,
            )
        payload = blob[2:]
        self.bm.mem_set(tuneheader, payload)
        return tuneheader + len(payload)

    # ---- play -------------------------------------------------------

    def play(self, hold_frames: int = 3) -> None:
        """Tap F1 (reset + play whole tune from beginning).

        ``hold_frames`` is the matrix-hold duration; the default of 3
        is enough for SID-Wizard's keyscan IRQ to observe the press
        and dispatch to ``f1er``.
        """
        self.bm.keymatrix_tap([(KEY.F1[0], KEY.F1[1])], frames=hold_frames)

    def stop(self, hold_frames: int = 3) -> None:
        """Tap F4 (stop / pause). Convenience for tearing down a capture."""
        # F4 is shift+F3 on the physical C64 matrix, but SID-Wizard
        # accepts the F4 alias (row 0 col 5 + LSHIFT). For a clean
        # "stop everything" we can also use RUN/STOP — simpler.
        self.bm.keymatrix_tap([(KEY.RUNSTOP[0], KEY.RUNSTOP[1])], frames=hold_frames)
