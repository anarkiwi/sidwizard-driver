"""Dump SID-Wizard's per-frame player ghost-register state to CSV."""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import tempfile

from vice_driver import BinMon, DiskMount, ViceContainer, ViceContainerError

from .fetch import fetch_disk1_d64
from .sidwizard import Sidwizard, SidwizardError

log = logging.getLogger("sidwizard_driver.ghost_dump")


# Player jump table: $1000=INITER, $1003=PLAYER (per-frame entry).
PLAYER_ENTRY = 0x1003

# anarkiwi/headlessvice's x64sc needs a writable $HOME/.local/state/vice.
VICE_STATE_DIR = "/root/.local/state/vice"

# Zero-page region covering per-voice ghost-register blocks plus
# FREQMOD/vibrato state. Over-dumps a bit ($10..$80, ~0x71 bytes/frame).
ZP_DUMP_START = 0x10
ZP_DUMP_END = 0x80

# Range scanned for SID-Wizard's filter-program self-modifying operands
# (FLTCTRL / FLTPOSI / CWEPCNT). The 1-SID editor build's player code
# lives between $1000 and ~$1FFF; the FilterProgram macro is emitted
# once, somewhere in that block. We start a few bytes past the
# jump-table to skip the inisub/playsub/mulpsub stubs.
SELFMOD_SCAN_START = 0x1010
SELFMOD_SCAN_LEN = 0x1000

# Labels for the extra player-code bytes carried in the ghost CSV
# alongside the ZP dump. These point at the operand byte of a
# self-modifying instruction (= 1 byte past the opcode).
SELFMOD_LABELS = ("FLTCTRL", "FLTPOSI", "CWEPCNT")

# Per-voice DETUNER labels. DETUNER lives in SID-Wizard's CONST_VAR
# region (= ZP, not part of the INIPVAR-cleared VARIABLES). It's read
# by WRPITCH as part of the ``adc DETUNER,x`` 8-bit ADC chain that
# produces the SID FREQ_LO write. Its base ZP address is build-dependent
# (CONST_VAR placement varies with feature flags), so we discover it
# from the WRPITCH instruction byte sequence.
DETUNER_LABELS = ("DETUNER_v0", "DETUNER_v1", "DETUNER_v2")


# Variable name map for the per-voice zero-page region. Labels are for
# v0; v1 = +7, v2 = +14. Gaps are left unannotated but still dumped.
PLAYER_ZP_LABELS = {
    0x10: "FREQLO",
    0x11: "FREQHI",
    0x12: "PWLOGHO",
    0x13: "PWHIGHO",
    0x14: "WFGHOST",
    0x15: "PTNGATE",
    0x16: "PWEEPCNT",
    0x26: "PACKCNT",
    0x27: "SPDCNT",
    0x28: "SEQPOS",
    0x29: "PTNPOS",
    0x2A: "WFTPOS",
    0x2B: "PWTPOS",
    0x2C: "ARPSCNT",
    0x3A: "CURPTN",
    0x3B: "CURNOT",
    0x3C: "DPITCH",
    0x3D: "CURIFX",
    0x3E: "CURINS",
    0x3F: "CURFX2",
    0x40: "CURVAL",
    0x4F: "SLIDEVIB",
    0x50: "FREQMODL",
    0x51: "FREQMODH",
    0x52: "VIDELCNT",
    0x53: "VIBFREQU",
    0x54: "VIBRACNT",
    0x55: "TRANSP",
    0x68: "CURCHORD",
    0x69: "CHORDPOS",
}

PER_VOICE_STRIDE = 7
N_VOICES = 3


def annotated_name(addr: int) -> str:
    for base, name in PLAYER_ZP_LABELS.items():
        for v in range(N_VOICES):
            if base + v * PER_VOICE_STRIDE == addr:
                return f"{name}_v{v}"
    return ""


def find_filter_selfmod_addrs(player_code: bytes, base_addr: int) -> tuple[int, int, int]:
    """Locate SID-Wizard's filter-program self-modifying operand bytes
    inside a dump of the loaded player code.

    SID-Wizard's player.asm (FilterProgram macro) emits this unique
    instruction sequence inside its filter routine::

        FLTCTRL cpx #selfmod    ; E0 ??
                bne SwUpEnd     ; D0 ??
        FLTPOSI ldy #selfmod    ; A0 ??
                lda (PLAYERZP),y ; B1 ??
                bmi NOCWEEP     ; 30 ??
        FISWEEP iny             ; C8
        CWEPCNT cmp #selfmod    ; C9 ??
                beq FLADVAN     ; F0 ??
                inc CWEPCNT+1   ; EE LO HI

    The 18-byte signature (with three wildcard operands and a free
    relative branch) is unique enough in the player code that scanning
    locates it reliably. The trailing ``inc CWEPCNT+1`` doubles as a
    sanity check: the absolute target of the ``inc`` must equal the
    address we computed for the CWEPCNT operand.

    Returns ``(fltctrl_addr, fltposi_addr, cwepcnt_addr)`` — the
    absolute addresses of the THREE self-modifying operand bytes,
    each one byte past its respective opcode.

    Raises ``ValueError`` if the signature is not found, or if the
    ``inc`` sanity check disagrees.
    """
    for i in range(len(player_code) - 17):
        if (
            player_code[i] == 0xE0  # cpx #imm
            and player_code[i + 2] == 0xD0  # bne rel
            and player_code[i + 4] == 0xA0  # ldy #imm
            and player_code[i + 6] == 0xB1  # lda (zp),y
            and player_code[i + 8] == 0x30  # bmi rel
            and player_code[i + 10] == 0xC8  # iny
            and player_code[i + 11] == 0xC9  # cmp #imm
            and player_code[i + 13] == 0xF0  # beq rel
            and player_code[i + 15] == 0xEE  # inc abs
        ):
            fltctrl = base_addr + i + 1
            fltposi = base_addr + i + 5
            cwepcnt = base_addr + i + 12
            inc_target = player_code[i + 16] | (player_code[i + 17] << 8)
            if inc_target != cwepcnt:
                raise ValueError(
                    f"FLTPOSI signature matched at offset {i} but the "
                    f"`inc abs` target ${inc_target:04X} disagrees with the "
                    f"computed CWEPCNT address ${cwepcnt:04X}"
                )
            return fltctrl, fltposi, cwepcnt
    raise ValueError("filter-program self-mod signature not found in player code")


def find_detuner_base_addr(player_code: bytes, base_addr: int) -> int:
    """Locate the ZP address of ``DETUNER`` (v0) inside a dump of the
    loaded player code.

    SID-Wizard's player.asm WRPITCH (line ~2051) begins::

        WRPITCH lda FREQLO,x       ; B5 10  (zp,X — FREQLO at $10)
                adc DETUNER,x      ; 75 ??  (zp,X — ?? is DETUNER's ZP addr)

    The pair ``B5 10 75 ??`` is unique to WRPITCH in the player code
    (no other site does ``lda $10,X ; adc <zp>,X``). Returns the
    discovered ZP address of DETUNER_v0; the per-voice stride is 7 so
    v1 = base+7, v2 = base+14.

    Raises ``ValueError`` if the signature is not found.
    """
    for i in range(len(player_code) - 3):
        if (
            player_code[i] == 0xB5      # lda zp,X
            and player_code[i + 1] == 0x10  # zp = $10 = FREQLO
            and player_code[i + 2] == 0x75  # adc zp,X
        ):
            detuner_zp = player_code[i + 3]
            # Sanity: DETUNER must be in ZP (= < $100). Also rule out
            # the obvious low-ZP region used by the per-voice VARIABLES
            # block ($10..$5F) — DETUNER lives in CONST_VAR which is
            # above ENDVARIABLES.
            if 0x60 <= detuner_zp <= 0xFE:
                return detuner_zp
            raise ValueError(
                f"WRPITCH signature matched but DETUNER operand "
                f"${detuner_zp:02X} is outside the expected ZP range "
                f"$60..$FE — likely a false-positive match at "
                f"offset ${base_addr + i:04X}"
            )
    raise ValueError("WRPITCH (lda FREQLO; adc DETUNER) signature not found")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--d64", default=None, help="SID-Wizard editor .d64 (auto-fetched if omitted)")
    p.add_argument("--swm", required=True, help="SWM module to play")
    p.add_argument("--frames", type=int, default=60)
    p.add_argument("--out", required=True, help="output CSV path")
    p.add_argument("--annotate", action="store_true", help="add SID-Wizard variable names")
    p.add_argument("--image", default="anarkiwi/headlessvice:latest")
    p.add_argument("--port", type=int, default=6502)
    p.add_argument("--idle-timeout", type=float, default=60.0)
    p.add_argument("--load-timeout", type=float, default=10.0)
    p.add_argument(
        "--mute-editor",
        action="store_true",
        help="zero $D400-$D418 at the pre-F1 checkpoint",
    )
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p.parse_args(argv)


def _discover_selfmod_addrs(bm: BinMon) -> tuple[int, int, int]:
    """Read the player code from VICE and locate FLTCTRL / FLTPOSI /
    CWEPCNT. Wrapper around :func:`find_filter_selfmod_addrs` that
    handles the live ``BinMon`` read.

    Must be called while VICE is halted (typical caller is
    ``_dump_loop`` between the first ``run_until_pc`` and the first ZP
    capture).
    """
    code = bm.mem_get(SELFMOD_SCAN_START, SELFMOD_SCAN_START + SELFMOD_SCAN_LEN - 1)
    return find_filter_selfmod_addrs(bytes(code), SELFMOD_SCAN_START)


def _discover_detuner_addrs(bm: BinMon) -> tuple[int, int, int]:
    """Read the player code from VICE and locate the per-voice DETUNER
    ZP addresses. Returns ``(DETUNER_v0, DETUNER_v1, DETUNER_v2)`` —
    stride 7 starting from the base discovered via
    :func:`find_detuner_base_addr`.

    Must be called while VICE is halted (typical caller is
    ``_dump_loop`` between the first ``run_until_pc`` and the first ZP
    capture).
    """
    code = bm.mem_get(SELFMOD_SCAN_START, SELFMOD_SCAN_START + SELFMOD_SCAN_LEN - 1)
    base = find_detuner_base_addr(bytes(code), SELFMOD_SCAN_START)
    return base, base + 7, base + 14


def _dump_loop(bm: BinMon, frames: int) -> tuple[
    list[tuple[int, bytes]],
    "tuple[int, int, int] | None",
    list[bytes],
    "tuple[int, int, int] | None",
    list[bytes],
]:
    """Run ``frames`` PLAYER ticks; capture ZP + filter self-mod bytes
    + per-voice DETUNER bytes per frame.

    Returns ``(zp_snapshots, selfmod_addrs, selfmod_snapshots,
    detuner_addrs, detuner_snapshots)``:

    * ``zp_snapshots`` — ``[(frame, zp_bytes), ...]`` (per-frame ZP
      window, same as before).
    * ``selfmod_addrs`` — ``(fltctrl_addr, fltposi_addr, cwepcnt_addr)``
      or ``None`` if discovery was skipped (frames=0).
    * ``selfmod_snapshots`` — list of length ``frames``; each entry is
      a 3-byte sequence ``(fltctrl_val, fltposi_val, cwepcnt_val)`` for
      that frame.
    * ``detuner_addrs`` — ``(DETUNER_v0_zp, DETUNER_v1_zp,
      DETUNER_v2_zp)`` or ``None``. These are ZP addresses (in $60..$FE)
      discovered from the WRPITCH instruction's ``adc DETUNER,X``
      operand.
    * ``detuner_snapshots`` — list of length ``frames``; each entry is
      a 3-byte ``(DETUNER_v0, DETUNER_v1, DETUNER_v2)`` capture.
    """
    zp_snapshots: list[tuple[int, bytes]] = []
    selfmod_snapshots: list[bytes] = []
    detuner_snapshots: list[bytes] = []
    selfmod_addrs: "tuple[int, int, int] | None" = None
    detuner_addrs: "tuple[int, int, int] | None" = None
    for frame in range(frames):
        bm.run_until_pc(PLAYER_ENTRY)
        with bm.halted():
            if selfmod_addrs is None:
                selfmod_addrs = _discover_selfmod_addrs(bm)
            if detuner_addrs is None:
                detuner_addrs = _discover_detuner_addrs(bm)
            zp = bm.mem_get(ZP_DUMP_START, ZP_DUMP_END)
            sm_bytes = bytes(bm.mem_get(addr, addr)[0] for addr in selfmod_addrs)
            det_bytes = bytes(bm.mem_get(addr, addr)[0] for addr in detuner_addrs)
        zp_snapshots.append((frame, bytes(zp)))
        selfmod_snapshots.append(sm_bytes)
        detuner_snapshots.append(det_bytes)
    return (
        zp_snapshots, selfmod_addrs, selfmod_snapshots,
        detuner_addrs, detuner_snapshots,
    )


def _write_csv(
    zp_snapshots: list[tuple[int, bytes]],
    out_path: str,
    annotate: bool,
    selfmod_addrs: "tuple[int, int, int] | None" = None,
    selfmod_snapshots: "list[bytes] | None" = None,
    detuner_addrs: "tuple[int, int, int] | None" = None,
    detuner_snapshots: "list[bytes] | None" = None,
) -> int:
    """Write the per-frame ghost dump to CSV.

    Each frame produces (ZP-byte-count) rows for the ZP window plus,
    when ``selfmod_addrs`` is provided, three additional rows for the
    FLTCTRL / FLTPOSI / CWEPCNT player-code operand bytes. When
    ``detuner_addrs`` is provided, three more rows for the per-voice
    DETUNER ZP values. All extra rows use the same
    ``frame,addr,value[,label]`` schema as the ZP rows.
    """
    rows = 0
    with open(out_path, "w", newline="") as fp:
        w = csv.writer(fp)
        header = ["frame", "addr", "value"]
        if annotate:
            header.append("label")
        w.writerow(header)
        for snap_idx, (frame, data) in enumerate(zp_snapshots):
            for i, val in enumerate(data):
                addr = ZP_DUMP_START + i
                row: list[object] = [frame, addr, val]
                if annotate:
                    row.append(annotated_name(addr))
                w.writerow(row)
                rows += 1
            if selfmod_addrs is not None and selfmod_snapshots is not None:
                sm_bytes = selfmod_snapshots[snap_idx]
                for addr, val, label in zip(selfmod_addrs, sm_bytes, SELFMOD_LABELS, strict=True):
                    row = [frame, addr, val]
                    if annotate:
                        row.append(label)
                    w.writerow(row)
                    rows += 1
            if detuner_addrs is not None and detuner_snapshots is not None:
                det_bytes = detuner_snapshots[snap_idx]
                for addr, val, label in zip(
                    detuner_addrs, det_bytes, DETUNER_LABELS, strict=True
                ):
                    row = [frame, addr, val]
                    if annotate:
                        row.append(label)
                    w.writerow(row)
                    rows += 1
    return rows


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - live VICE
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if not args.d64:
        args.d64 = str(fetch_disk1_d64())
    if not os.path.isfile(args.d64):
        print(f"not a file: {args.d64}", file=sys.stderr)
        return 2
    if not os.path.isfile(args.swm):
        print(f"not a file: {args.swm}", file=sys.stderr)
        return 2

    host_work_dir = tempfile.mkdtemp(prefix="sidwizard-driver-ghost-")
    container_work_dir = "/tmp/sidwizard-driver"
    host_swm_d64 = os.path.join(host_work_dir, "tune.d64")
    container_swm_d64 = f"{container_work_dir}/tune.d64"
    container_d64 = "/tmp/sidwizard-editor.d64"
    host_vice_state = tempfile.mkdtemp(prefix="sidwizard-driver-ghost-vice-")

    mounts = [
        DiskMount(host_path=args.d64, container_path=container_d64, read_only=True),
        DiskMount(host_path=host_work_dir, container_path=container_work_dir, read_only=False),
        DiskMount(host_path=host_vice_state, container_path=VICE_STATE_DIR, read_only=False),
    ]
    container = ViceContainer(
        image=args.image,
        entrypoint="x64sc",
        binmon_port=args.port,
        autostart=container_d64,
        mounts=mounts,
        warp=True,
    )

    try:
        with container:
            with BinMon(port=args.port) as bm:
                bm.exit()
                sw = Sidwizard(bm)
                tuneheader = sw.wait_for_idle(timeout=args.idle_timeout)
                log.info("TUNEHEADER = $%04X", tuneheader)

                sw.load_swm_via_menu(
                    swm_path=args.swm,
                    host_d64_path=host_swm_d64,
                    container_d64_path=container_swm_d64,
                    tuneheader=tuneheader,
                    load_timeout=args.load_timeout,
                )

                # Pre-arm a stop-on-hit checkpoint at PLAYER_ENTRY before
                # F1 so we halt on the first $1003 dispatch (song frame 0)
                # rather than letting warp run dozens of frames first.
                pre_cp = bm.checkpoint_set(PLAYER_ENTRY, stop_when_hit=True)
                sw.play()

                if args.mute_editor:
                    sw.clear_sid_registers()
                bm.checkpoint_delete(pre_cp.checknum)

                (
                    zp_snapshots,
                    selfmod_addrs,
                    selfmod_snapshots,
                    detuner_addrs,
                    detuner_snapshots,
                ) = _dump_loop(bm, args.frames)
                if selfmod_addrs is not None:
                    log.info(
                        "filter self-mod addrs: FLTCTRL=$%04X FLTPOSI=$%04X " "CWEPCNT=$%04X",
                        *selfmod_addrs,
                    )
                if detuner_addrs is not None:
                    log.info(
                        "DETUNER ZP addrs: v0=$%02X v1=$%02X v2=$%02X",
                        *detuner_addrs,
                    )

    except ViceContainerError as e:
        print(f"VICE container error: {e}", file=sys.stderr)
        return 4
    except SidwizardError as e:
        print(f"SID-Wizard error: {e}", file=sys.stderr)
        return 5

    rows = _write_csv(
        zp_snapshots,
        args.out,
        annotate=args.annotate,
        selfmod_addrs=selfmod_addrs,
        selfmod_snapshots=selfmod_snapshots,
        detuner_addrs=detuner_addrs,
        detuner_snapshots=detuner_snapshots,
    )
    print(f"wrote {rows} rows to {args.out} (workdir preserved at {host_work_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
