"""High-level driver for SID-Wizard running inside asid-vice."""

from __future__ import annotations

import logging
import time
from typing import Optional

from vice_driver import KEY, BinMon, parse_screen_response
from vice_driver.binmon import TAP_MODE_FIXED
from vice_driver.expect import Expect, verify

from .d64 import write_d64_with_swm

log = logging.getLogger(__name__)


STARTUP_MENU_MARKER = "STARTUP-MENU"

TUNEHEADER_AUTHOR_OFFSET = 0x18

EDITOR_SCAN_LO = 0x0800
EDITOR_SCAN_HI = 0xC000

# loadtun signature:
#   ldx #<TUNEHEADER ; ldy #>TUNEHEADER ; lda #$00 ; jsr KERNAL.LOAD
# Not unique in SID-Wizard 1.94 — loadins emits the same pattern with a
# different immediate. Disambiguate by checking which candidate points at
# SWM1 magic: only the real TUNEHEADER does.
LOADTUN_TAIL = bytes([0xA9, 0x00, 0x20, 0xD5, 0xFF])
LOADTUN_LDX = 0xA2
LOADTUN_LDY = 0xA0
SWM_MAGIC = b"SWM1"


# wasjamm resetJamIns loop signature (playadapter.inc):
#   lda #0 ; sta wasjamm,x ; txa ; sec ; sbc #7 ; tax ; bpl -
WASJAMM_PATTERN_HEAD = bytes([0xA9, 0x00, 0x9D])
WASJAMM_PATTERN_TAIL = bytes([0x8A, 0x38, 0xE9, 0x07, 0xAA, 0x10])
WASJAMM_PATTERN_LEN = len(WASJAMM_PATTERN_HEAD) + 2 + len(WASJAMM_PATTERN_TAIL)

VOICE_STRIDE = 7
DEFAULT_VOICE_COUNT = 3


class SidwizardError(RuntimeError):
    pass


class Sidwizard:
    """High-level driver for a running SID-Wizard inside asid-vice."""

    def __init__(self, bm: BinMon):
        self.bm = bm

    def wait_for_startup_menu(  # pragma: no cover - requires live VICE
        self, timeout: float = 30.0, poll_interval: float = 0.5
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = parse_screen_response(self.bm.screen_get())
            if STARTUP_MENU_MARKER in snap.text():
                return
            time.sleep(poll_interval)
        raise SidwizardError(f"timed out after {timeout:.0f}s waiting for SID-Wizard startup menu")

    def dismiss_startup_menu(self) -> None:  # pragma: no cover - requires live VICE
        self._tap([KEY.RETURN])

    def wait_for_editor(  # pragma: no cover - requires live VICE
        self, timeout: float = 30.0, poll_interval: float = 0.5
    ) -> int:
        deadline = time.monotonic() + timeout
        last_err: Optional[SidwizardError] = None
        while time.monotonic() < deadline:
            try:
                return self.discover_tuneheader()
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
        half = timeout / 2
        self.wait_for_startup_menu(timeout=half, poll_interval=poll_interval)
        self.dismiss_startup_menu()
        return self.wait_for_editor(timeout=half, poll_interval=poll_interval)

    def discover_tuneheader(self) -> int:
        ram = self.bm.mem_get(EDITOR_SCAN_LO, EDITOR_SCAN_HI - 1)
        candidates: list[int] = []
        for i in range(len(ram) - (4 + len(LOADTUN_TAIL)) + 1):
            if (
                ram[i] == LOADTUN_LDX
                and ram[i + 2] == LOADTUN_LDY
                and ram[i + 4 : i + 4 + len(LOADTUN_TAIL)] == LOADTUN_TAIL
            ):
                lo = ram[i + 1]
                hi = ram[i + 3]
                candidates.append(lo | (hi << 8))
        if not candidates:
            raise SidwizardError(
                "loadtun signature not found in editor RAM "
                f"({EDITOR_SCAN_LO:#06x}-{EDITOR_SCAN_HI:#06x}); "
                "editor may not have finished loading"
            )
        unique = sorted(set(candidates))
        if len(unique) == 1:
            return unique[0]

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

    def discover_wasjamm(self) -> int:
        ram = self.bm.mem_get(EDITOR_SCAN_LO, EDITOR_SCAN_HI - 1)
        head = WASJAMM_PATTERN_HEAD
        tail = WASJAMM_PATTERN_TAIL
        tail_offset = len(head) + 2
        candidates: set[int] = set()
        for i in range(len(ram) - WASJAMM_PATTERN_LEN + 1):
            if ram[i : i + len(head)] != head:
                continue
            if ram[i + tail_offset : i + tail_offset + len(tail)] != tail:
                continue
            lo = ram[i + len(head)]
            hi = ram[i + len(head) + 1]
            addr = lo | (hi << 8)
            if not EDITOR_SCAN_LO <= addr < EDITOR_SCAN_HI:
                continue
            candidates.add(addr)
        if not candidates:
            raise SidwizardError(
                "wasjamm resetJamIns signature not found in editor RAM "
                f"({EDITOR_SCAN_LO:#06x}-{EDITOR_SCAN_HI:#06x})"
            )
        if len(candidates) > 1:
            raise SidwizardError(
                f"wasjamm signature is ambiguous: "
                f"{', '.join(f'${a:04X}' for a in sorted(candidates))}"
            )
        return next(iter(candidates))

    def clear_sid_registers(self) -> None:
        """Zero $D400-$D418. Call while halted at the pre-F1 checkpoint
        so the player's first tick writes onto a clean register file."""
        self.bm.mem_set(0xD400, b"\x00" * 0x19)

    def mute_editor_voices(
        self,
        voice_count: int = DEFAULT_VOICE_COUNT,
        clear_sid: bool = True,
    ) -> int:
        """Zero ``wasjamm[v]`` for v in 0..voice_count-1, optionally
        clearing $D400-$D418 too. For race-free pre-song muting prefer
        :meth:`clear_sid_registers` at the pre-F1 checkpoint halt."""
        if voice_count < 1:
            raise ValueError(f"voice_count must be >= 1, got {voice_count}")
        wasjamm = self.discover_wasjamm()
        for v in range(voice_count):
            self.bm.mem_set(wasjamm + v * VOICE_STRIDE, b"\x00")
        if clear_sid:
            self.clear_sid_registers()
        return wasjamm

    def play(self) -> None:  # pragma: no cover - requires live VICE
        self._tap([KEY.F1])

    def _tap(  # pragma: no cover - requires live VICE
        self, keys: list[tuple[int, int]], frames: int = 8, settle: float = 0.2
    ) -> None:
        # TAP_MODE_FIXED with 8 frames + settle — observed-mode taps
        # silently miss SID-Wizard's keyscan IRQ under warp.
        self.bm.keymatrix_tap(keys, mode=TAP_MODE_FIXED, frames=frames)
        time.sleep(frames * 0.02 + settle)

    def enter_load_menu(self) -> None:  # pragma: no cover - requires live VICE
        self._tap([KEY.LSHIFT, KEY.F7])
        self._tap([KEY.CRSRUD])
        self._tap([KEY.RETURN])

    FILENAME_DIALOG_PREFIX = "FILENAME:"
    FILENAME_DIALOG_SUFFIX_MARKER = "(FILETYPE"

    def _current_filename_selection(self) -> str | None:
        snap = parse_screen_response(self.bm.screen_get())
        row0 = snap.lines()[0]
        if self.FILENAME_DIALOG_PREFIX not in row0:
            return None
        try:
            after = row0.split(self.FILENAME_DIALOG_PREFIX, 1)[1]
            before = after.split(self.FILENAME_DIALOG_SUFFIX_MARKER, 1)[0]
        except IndexError:
            return None
        name = before.strip().rstrip(".").strip()
        return name.upper() if name else None

    def load_swm_via_menu(  # pragma: no cover - requires live VICE
        self,
        swm_path: str,
        host_d64_path: str,
        container_d64_path: str,
        tuneheader: int,
        load_timeout: float = 10.0,
        max_nav_steps: int = 32,
    ) -> None:
        on_disk_name = write_d64_with_swm(host_d64_path, swm_path)
        self.bm.attach_drive(container_d64_path, unit=8, drive=0)

        target = on_disk_name.upper()
        if target.endswith(".SWM"):
            target = target[:-4]

        watch_addr = tuneheader + TUNEHEADER_AUTHOR_OFFSET
        pre_byte = self.bm.mem_get(watch_addr, watch_addr)[0]

        self.enter_load_menu()

        deadline = time.monotonic() + load_timeout
        seen: list[str] = []
        current = self._current_filename_selection()
        for _ in range(max_nav_steps):
            current = self._current_filename_selection()
            if current is None:
                if time.monotonic() > deadline:
                    raise SidwizardError(
                        f"loadtun dialog did not produce a FILENAME line within {load_timeout:.1f}s"
                    )
                time.sleep(0.1)
                continue
            if current == target:
                break
            if seen and current == seen[-1]:
                raise SidwizardError(
                    f"loadtun dialog wrapped without finding {target!r}; saw {seen}"
                )
            seen.append(current)
            self._tap([KEY.CRSRUD])
        else:
            raise SidwizardError(
                f"loadtun dialog: {max_nav_steps} CRSRDOWN steps did not "
                f"reach {target!r} (saw {seen + [current]})"
            )

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

        # The author byte flips when KERNAL.LOAD + depackt finish, but
        # the file dialog stays up ~0.7s longer; F1 during that window
        # is consumed by the dialog instead of triggering f1er.
        self._wait_for_loadtun_dialog_dismissed(load_timeout)

    def _wait_for_loadtun_dialog_dismissed(  # pragma: no cover - requires live VICE
        self, timeout: float, poll_interval: float = 0.1
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = parse_screen_response(self.bm.screen_get())
            if self.FILENAME_DIALOG_PREFIX not in snap.lines()[0]:
                return
            time.sleep(poll_interval)
        raise SidwizardError(
            f"loadtun dialog still visible after {timeout:.1f}s "
            f"(post-load auto-dismiss did not happen)"
        )

    def cycle(self) -> int:  # pragma: no cover - requires live VICE
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
