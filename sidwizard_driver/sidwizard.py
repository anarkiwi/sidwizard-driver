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

from vice_driver import KEY, BinMon, lookup, parse_screen_response, text_to_chords
from vice_driver.binmon import TAP_MODE_FIXED
from vice_driver.expect import Expect, verify

from .d64 import write_d64_with_swm

log = logging.getLogger(__name__)


# Text marker visible on the SID-Wizard 1.94 startup-menu screen.
# Shown after the bootloader paints the player-selection UI and
# before the user picks a player — see
# native/sources/include/startupmenu.inc.
STARTUP_MENU_MARKER = "STARTUP-MENU"

# Offset inside the SWM file header where the AUTHOR field starts.
# Mirrors pysidwizard.constants.AUTHOR_POS. The editor writes the
# author info for the live tune into TUNEHEADER + this offset, so
# watching the first byte here is a cheap "did a new tune just land?"
# signal — distinct tunes almost always have distinct author strings,
# and even the default-empty-tune-vs-loaded transition flips this byte.
TUNEHEADER_AUTHOR_OFFSET = 0x18

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
#
# The signature is NOT unique in SID-Wizard 1.94 — `loadins` (instrument
# load) and one other call site emit the same pattern with different
# immediates. Disambiguation: TUNEHEADER's contents start with the
# ``SWM1`` magic once the editor has initialised an empty default tune
# at boot (per the CheckSWM routine — the editor itself relies on this
# invariant). So among the candidates we filter to the one whose
# pointee begins with ``SWM1``.
LOADTUN_TAIL = bytes([0xA9, 0x00, 0x20, 0xD5, 0xFF])
LOADTUN_LDX = 0xA2
LOADTUN_LDY = 0xA0
SWM_MAGIC = b"SWM1"


class SidwizardError(RuntimeError):
    pass


class Sidwizard:
    """High-level driver for a running SID-Wizard inside asid-vice."""

    def __init__(self, bm: BinMon):
        self.bm = bm

    # ---- boot --------------------------------------------------------
    #
    # SID-Wizard 1.94's `disk1.d64` autostart unfolds in two phases:
    #   1. A small bootloader displays a startup-menu screen
    #      ("STARTUP-MENU") asking the user to pick a player variant
    #      (sidwiz / sidwiz2 / sidwiz3 / sidwiz4) and a tuning.
    #   2. After the user presses RETURN, the bootloader loads the
    #      selected editor PRG into RAM and jumps to it — that's the
    #      tracker UI.
    #
    # Surprisingly, the editor does NOT hook the $0314/$0315 IRQ
    # vector. It installs its player as a raster IRQ via the hardware
    # $FFFE vector and CIA timer, leaving the KERNAL's $EA31 visible
    # at $0314. So the "is the editor alive" signal we use is the
    # `discover_tuneheader()` byte scan — it only matches once the
    # editor PRG has been loaded into RAM.

    def wait_for_startup_menu(  # pragma: no cover - requires live VICE
        self, timeout: float = 30.0, poll_interval: float = 0.5
    ) -> None:
        """Block until the SID-Wizard bootloader's startup-menu screen
        is visible. Used after autostart, before tapping RETURN to
        select a player."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = parse_screen_response(self.bm.screen_get())
            if STARTUP_MENU_MARKER in snap.text():
                log.info("SID-Wizard startup menu visible")
                return
            time.sleep(poll_interval)
        raise SidwizardError(f"timed out after {timeout:.0f}s waiting for SID-Wizard startup menu")

    def dismiss_startup_menu(self) -> None:  # pragma: no cover - requires live VICE
        """Tap RETURN to confirm the default player selection and load
        the editor PRG. Idempotent in the sense that pressing RETURN on
        the editor screen is also harmless (RETURN is the pattern-row
        commit key in the editor)."""
        self._tap([KEY.RETURN])

    def wait_for_editor(  # pragma: no cover - requires live VICE
        self, timeout: float = 30.0, poll_interval: float = 0.5
    ) -> int:
        """Block until the editor PRG is loaded into RAM and the
        ``loadtun`` byte signature can be found. Returns the discovered
        ``TUNEHEADER`` address."""
        deadline = time.monotonic() + timeout
        last_err: Optional[SidwizardError] = None
        while time.monotonic() < deadline:
            try:
                addr = self.discover_tuneheader()
                log.info("editor alive; TUNEHEADER = $%04X", addr)
                return addr
            except SidwizardError as e:
                last_err = e
                time.sleep(poll_interval)
        raise SidwizardError(
            f"timed out after {timeout:.0f}s waiting for editor PRG to load"
            f" (last signature error: {last_err})"
        )

    def wait_for_idle(  # pragma: no cover - requires live VICE
        self, timeout: float = 60.0, poll_interval: float = 0.5
    ) -> int:
        """Drive the bootloader through to the live editor.

        Equivalent to: wait_for_startup_menu → dismiss_startup_menu →
        wait_for_editor. Returns the discovered ``TUNEHEADER`` address
        as a side-effect; callers that want to skip the dual call can
        ignore the return value.
        """
        half = timeout / 2
        self.wait_for_startup_menu(timeout=half, poll_interval=poll_interval)
        self.dismiss_startup_menu()
        return self.wait_for_editor(timeout=half, poll_interval=poll_interval)

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
        unique = sorted(set(candidates))
        if len(unique) == 1:
            return unique[0]

        # Multiple candidates — disambiguate by checking which one
        # points at SWM1 magic bytes. The editor's init code writes
        # "SWM1" to TUNEHEADER+0 as part of the empty-tune setup, so
        # the right candidate is the one whose first 4 bytes match.
        valid = [addr for addr in unique if self.bm.mem_get(addr, addr + 3) == SWM_MAGIC]
        if len(valid) == 1:
            return valid[0]
        if not valid:
            raise SidwizardError(
                f"loadtun signature found at {len(unique)} addresses but "
                f"none point at SWM1 magic: "
                f"{', '.join(f'${a:04X}' for a in unique)}"
            )
        raise SidwizardError(
            f"loadtun signature is ambiguous AND multiple candidates "
            f"point at SWM1: {', '.join(f'${a:04X}' for a in valid)}"
        )

    # ---- side load --------------------------------------------------

    def side_load_swm(  # pragma: no cover - requires live VICE
        self, swm_path: str, tuneheader: int
    ) -> int:
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

    def play(self, hold_frames: int = 3) -> None:  # pragma: no cover - requires live VICE
        """Tap F1 (reset + play whole tune from beginning).

        ``hold_frames`` is the matrix-hold duration; the default of 3
        is enough for SID-Wizard's keyscan IRQ to observe the press
        and dispatch to ``f1er``.
        """
        self.bm.keymatrix_tap([(KEY.F1[0], KEY.F1[1])], frames=hold_frames)

    def stop(self, hold_frames: int = 3) -> None:  # pragma: no cover - requires live VICE
        """Tap F4 (stop / pause). Convenience for tearing down a capture."""
        # F4 is shift+F3 on the physical C64 matrix, but SID-Wizard
        # accepts the F4 alias (row 0 col 5 + LSHIFT). For a clean
        # "stop everything" we can also use RUN/STOP — simpler.
        self.bm.keymatrix_tap([(KEY.RUNSTOP[0], KEY.RUNSTOP[1])], frames=hold_frames)

    # ---- disk-menu load (path B) ------------------------------------
    #
    # The "side-load to TUNEHEADER" shortcut needs a separate depack +
    # init pass we don't yet implement. Path B routes around it by
    # driving SID-Wizard's own loadtun, which calls KERNAL.LOAD →
    # depackt → dispaut → subtune reset in one shot.
    #
    # Steps (see native/sources/include/menu.inc and keyhandler.inc):
    #   1. attach a fresh single-file .d64 holding the .swm to drive 8
    #   2. tap SHIFT+F7 → menuer (line 2090 in keyhandler.inc)
    #   3. tap CRSRDOWN once → cursor lands on loadtun (menupoint 2)
    #   4. tap RETURN → enter file dialog (filename-typer subwindow)
    #   5. type the filename without extension (SID-Wizard's regname
    #      appends ``.SWM`` before calling OPEN — see menu.inc:1735)
    #   6. tap RETURN → load runs through KERNAL.LOAD → depackt
    #   7. wait for the load + in-place depack to finish

    def _tap(  # pragma: no cover - requires live VICE
        self, keys: list[tuple[int, int]], frames: int = 8, settle: float = 0.2
    ) -> None:
        """Tap a chord and pause long enough for the editor's IRQ
        scanner to observe the press AND the subsequent release."""
        self.bm.keymatrix_tap(keys, mode=TAP_MODE_FIXED, frames=frames)
        # frames are PAL frames (~20 ms). Wall-clock wait covers tap
        # duration + editor reaction; settle adds a safety margin.
        time.sleep(frames * 0.02 + settle)

    def attach_swm_disk(  # pragma: no cover - requires live VICE
        self, swm_path: str, host_d64_path: str, container_d64_path: str
    ) -> None:
        """Build a fresh single-file .d64 holding ``swm_path`` and attach
        it to drive 8. ``host_d64_path`` is where the file is written on
        the host; ``container_d64_path`` is the same path as seen inside
        the asid-vice container (i.e. via a ``DiskMount``).

        Replaces whatever disk was previously at drive 8 — asid-vice's
        attach_drive detaches the old image first.
        """
        write_d64_with_swm(host_d64_path, swm_path)
        self.bm.attach_drive(container_d64_path, unit=8, drive=0)

    def enter_load_menu(self) -> None:  # pragma: no cover - requires live VICE
        """Open SID-Wizard's menu, navigate to loadtun, confirm.

        Sequence: SHIFT+F7 (menu) → CRSRDOWN (loadtun is below savetun)
        → RETURN (opens the file dialog, defaulting to the
        filename-typer subwindow).
        """
        self._tap([KEY.LSHIFT, KEY.F7])
        self._tap([KEY.CRSRUD])  # CRSRUD without SHIFT = "down"
        self._tap([KEY.RETURN])

    def type_filename(self, name: str) -> None:  # pragma: no cover - requires live VICE
        """Type ``name`` into the file dialog one chord at a time.

        Letters are upper-cased and sent via matrix taps; SID-Wizard's
        own keyscan reads the matrix directly so this is sufficient.
        Do NOT include the ``.SWM`` extension — the editor appends it
        in ``regname`` (menu.inc:1735) before opening the file.
        """
        for chord in text_to_chords(name):
            keys = [lookup(n) for n in chord]
            self._tap(keys)

    def load_swm_via_menu(  # pragma: no cover - requires live VICE
        self,
        swm_path: str,
        host_d64_path: str,
        container_d64_path: str,
        tuneheader: int,
        load_timeout: float = 10.0,
    ) -> None:
        """Full path-B load: build d64, attach, drive menu, type name,
        verify the load completed.

        Completion is detected by polling TUNEHEADER + author offset
        until the byte differs from the pre-load snapshot — i.e. a new
        tune's author string overwrote the previous one. The editor's
        loadtun → depackt → dispaut path runs in microseconds of warp
        time, so the verify usually returns on the first poll.

        Raises :class:`SidwizardError` if the byte never changes within
        ``load_timeout`` — almost always means the filename wasn't on
        the disk (FILE NOT FOUND) or the menu navigation desynced.
        """
        log.info("building d64 and attaching %s as drive 8", swm_path)
        on_disk_name = write_d64_with_swm(host_d64_path, swm_path)
        self.bm.attach_drive(container_d64_path, unit=8, drive=0)

        # Strip the ``.SWM`` extension when typing — the editor appends
        # it. Defensive against callers passing a name without the
        # extension by checking the suffix.
        typed = on_disk_name
        if typed.upper().endswith(".SWM"):
            typed = typed[:-4]

        # Snapshot the byte we'll watch BEFORE the load runs.
        watch_addr = tuneheader + TUNEHEADER_AUTHOR_OFFSET
        pre_byte = self.bm.mem_get(watch_addr, watch_addr)[0]
        log.debug("pre-load TUNEHEADER+$%02X = $%02X", TUNEHEADER_AUTHOR_OFFSET, pre_byte)

        log.info("entering load menu...")
        self.enter_load_menu()
        log.info("typing filename %r...", typed)
        self.type_filename(typed)
        log.info("tapping RETURN to load...")
        self._tap([KEY.RETURN])

        ok, observed = verify(
            self.bm,
            Expect(
                addr=watch_addr,
                want=lambda b, p=pre_byte: b != p,
                timeout=load_timeout,
                poll_interval=0.1,
            ),
        )
        if not ok:
            raise SidwizardError(
                f"load did not complete within {load_timeout:.1f}s "
                f"(TUNEHEADER+${TUNEHEADER_AUTHOR_OFFSET:02X} still "
                f"${observed:02X}; FILE NOT FOUND or menu desync?)"
            )
        log.info("load complete (author byte $%02X → $%02X)", pre_byte, observed)

    # ---- cycle-counter wait -----------------------------------------

    def cycle(self) -> int:  # pragma: no cover - requires live VICE
        """Return the absolute CPU cycle counter from the most recent
        ``cpuhistory`` entry."""
        history = self.bm.cpuhistory_get(count=1)
        if not history:
            raise SidwizardError("cpuhistory_get returned empty list")
        return history[0].cycle

    def wait_for_cycles(  # pragma: no cover - requires live VICE
        self,
        cycle_count: int,
        timeout: float = 120.0,
        poll_interval: float = 0.5,
    ) -> tuple[int, int]:
        """Block until at least ``cycle_count`` CPU cycles have elapsed
        from now. Returns ``(start_cycle, end_cycle)``.

        ``start_cycle`` is the absolute cycle counter sampled at the
        start of the wait — useful as the ``start_cycle`` argument to
        :func:`sidwizard_driver.dump.quantise_to_frames` so the
        emitted CSV's frame 0 corresponds to "now".

        ``timeout`` is wall-clock; warp mode typically runs at >>1x
        real-time so the default is generous.
        """
        start = self.cycle()
        target = start + cycle_count
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cur = self.cycle()
            if cur >= target:
                return start, cur
            time.sleep(poll_interval)
        cur = self.cycle()
        raise SidwizardError(
            f"wait_for_cycles({cycle_count}) timed out after {timeout:.1f}s; "
            f"only {cur - start} cycles elapsed"
        )
