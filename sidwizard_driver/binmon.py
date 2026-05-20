"""Binary-monitor wire protocol for asid-vice.

Implements the request/response framing documented in the asid-vice README and
the keymatrix/screenscrape opcode extensions (0x74-0x77). All multi-byte
fields are little-endian. STX = 0x02, API version = 0x02.

Public surface is the BinMon class. Every call() routes through one socket
and matches responses by request_id, so unsolicited STOPPED/RESUMED/JAM
events that arrive between requests are silently consumed and surfaced
via the on_event callback.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

STX = 0x02
API_VERSION = 0x02
REQ_HEADER_LEN = 11
RESP_HEADER_LEN = 12


class OPCODE:
    """Binmon opcode constants. Values verified against
    src/monitor/monitor_binary.c in asid-vice."""

    # Standard binmon
    MEM_GET = 0x01
    MEM_SET = 0x02
    CHECKPOINT_GET = 0x11
    CHECKPOINT_SET = 0x12
    CHECKPOINT_DELETE = 0x13
    CHECKPOINT_LIST = 0x14
    CHECKPOINT_TOGGLE = 0x15
    CONDITION_SET = 0x22
    REGISTERS_GET = 0x31
    REGISTERS_SET = 0x32
    DUMP = 0x41
    UNDUMP = 0x42
    RESOURCE_GET = 0x51
    RESOURCE_SET = 0x52
    ADVANCE_INSTRUCTIONS = 0x71
    KEYBOARD_FEED = 0x72  # ASCII into KERNAL buffer (does not touch matrix)
    EXECUTE_UNTIL_RETURN = 0x73
    PING = 0x81
    BANKS_AVAILABLE = 0x82
    REGISTERS_AVAILABLE = 0x83
    DISPLAY_GET = 0x84
    VICE_INFO = 0x85
    CPUHISTORY_GET = 0x86
    PALETTE_GET = 0x91
    JOYPORT_SET = 0xA2
    USERPORT_SET = 0xB2
    EXIT = 0xAA
    QUIT = 0xBB
    RESET = 0xCC
    AUTOSTART = 0xDD

    # asid-vice extensions (see asid-vice README)
    KEYMATRIX_SET = 0x74
    KEYMATRIX_TAP = 0x75
    KEYMATRIX_GET = 0x76
    SCREEN_GET = 0x77
    DRIVE_ATTACH = 0x78

    # Unsolicited events
    JAM = 0x61
    STOPPED = 0x62
    RESUMED = 0x63


# Tap modes for KEYMATRIX_TAP body:
TAP_MODE_OBSERVED = 0  # release on first observed CIA1 read of the injected bit
TAP_MODE_FIXED = 1  # hold for fixed N frames

# release_reason values from KEYMATRIX_GET body:
RELEASE_NONE = 0
RELEASE_OBSERVED = 1
RELEASE_TIMEOUT = 2
RELEASE_MANUAL = 3

# Checkpoint operation flags (montypes.h MEMORY_OP).
CHECK_LOAD = 0x01
CHECK_STORE = 0x02
CHECK_EXEC = 0x04

# Memspace selectors used by checkpoint_set / cpuhistory_get / mem_get.
# Verified against monitor_binary.c get_requested_memspace().
MEMSPACE_MAIN = 0  # e_comp_space (C64 main CPU)
MEMSPACE_DRIVE8 = 1
MEMSPACE_DRIVE9 = 2
MEMSPACE_DRIVE10 = 3
MEMSPACE_DRIVE11 = 4

ERR_OK = 0x00


class BinmonError(RuntimeError):
    """Raised when the emulator returns a non-zero error code or framing breaks."""


@dataclass
class BinmonResponse:
    opcode: int
    err: int
    req_id: int
    body: bytes


@dataclass
class Checkpoint:
    """Decoded CHECKPOINT_INFO (response 0x11) body — 23 bytes.

    Layout per monitor_binary_response_checkpoint_info():
      [0..3]   checknum (u32 LE) — opaque ID assigned by VICE
      [4]      hit flag (1 if this info is being delivered as a hit
               event, 0 if from a get/set/list reply)
      [5..6]   start_addr (u16 LE)
      [7..8]   end_addr (u16 LE), inclusive
      [9]      stop when hit
      [10]     enabled
      [11]     op bitmask (CHECK_LOAD|CHECK_STORE|CHECK_EXEC)
      [12]     temporary
      [13..16] hit_count (u32 LE)
      [17..20] ignore_count (u32 LE)
      [21]     has_condition
      [22]     memspace
    """

    checknum: int
    hit: bool
    start: int
    end: int
    stop_when_hit: bool
    enabled: bool
    op: int
    temporary: bool
    hit_count: int
    ignore_count: int
    has_condition: bool
    memspace: int


@dataclass
class CpuHistoryRecord:
    """One entry from CPUHISTORY_GET. Registers come back as a sparse
    dict keyed by VICE register id; well-known IDs are exposed as
    properties for ergonomic access. Only A/X/Y/SP/FLAGS/PC are
    populated by the cpuhistory path; rasterline/cycle slots are
    placeholders (0xFFFF) per monitor_binary_process_cpuhistory()."""

    cycle: int
    op: int  # opcode byte at PC
    p1: int  # operand byte 1
    p2: int  # operand byte 2
    registers: dict[int, int]

    # IDs are stable across VICE versions; values mirror monitor.h
    # e_A=0, e_X=1, e_Y=2, e_PC=3, e_SP=4, e_FLAGS=5, e_Rasterline=8, e_Cycle=9.
    @property
    def a(self) -> int:
        return self.registers.get(0, 0) & 0xFF

    @property
    def x(self) -> int:
        return self.registers.get(1, 0) & 0xFF

    @property
    def y(self) -> int:
        return self.registers.get(2, 0) & 0xFF

    @property
    def pc(self) -> int:
        return self.registers.get(3, 0) & 0xFFFF

    @property
    def sp(self) -> int:
        return self.registers.get(4, 0) & 0xFF

    @property
    def flags(self) -> int:
        return self.registers.get(5, 0) & 0xFF


@dataclass
class TapResult:
    keyarr: bytes  # 8 bytes — live matrix
    custom: int  # bitmap: bit0=RESTORE1, bit1=RESTORE2, bit2=CAPS, bit3=4080
    cia1_reads_total: int
    cia1_reads_sampling: int
    release_reason: int  # RELEASE_*
    n_keys: int
    frames_until_timeout: int


class BinMon:
    """Synchronous binmon client. One TCP socket per instance.

    Drains the initial STOPPED notification on connect and resumes the CPU
    by sending EXIT once. After that, every call() matches its response by
    request_id, so any unsolicited STOPPED/RESUMED/JAM that arrives between
    calls is consumed and (optionally) reported via on_event.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6502,
        on_event: Optional[Callable[[BinmonResponse], None]] = None,
        auto_resume: bool = True,
    ):
        """auto_resume: if True (default), every call() is followed by an
        EXIT opcode to leave the CPU running. asid-vice halts the CPU for
        every command, so without this the C64 makes no progress between
        polls (the autostart never finishes etc.). Set False if you need
        precise control over when the CPU runs (e.g. raster checkpoints)."""
        self.host = host
        self.port = port
        self.on_event = on_event
        self.auto_resume = auto_resume
        self._sock: Optional[socket.socket] = None
        self._req_id = 0
        self._lock = threading.Lock()

    # ---- lifecycle -----------------------------------------------------

    def connect(self, timeout: float = 5.0, attempts: int = 50, retry_delay: float = 0.2) -> None:
        """Open the socket. Retries on connection failure AND on early
        close-by-peer — docker-proxy accepts on the host port before the
        container's x64sc has bound inside, so the first few connects can
        complete-then-immediately-close."""
        last_err: Exception | None = None
        for _ in range(attempts):
            s: Optional[socket.socket] = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect((self.host, self.port))
                self._sock = s
                self._drain_unsolicited(0.3)
                return
            except (OSError, BinmonError) as e:
                last_err = e
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
                self._sock = None
                time.sleep(retry_delay)
        raise BinmonError(f"could not connect to binmon at {self.host}:{self.port}: {last_err}")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "BinMon":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- low-level framing ---------------------------------------------

    def _next_req(self) -> int:
        self._req_id = (self._req_id + 1) & 0xFFFFFFFF
        if self._req_id == 0:
            self._req_id = 1
        return self._req_id

    def _send(self, opcode: int, body: bytes, req_id: int) -> None:
        assert self._sock is not None
        header = struct.pack("<BBII B", STX, API_VERSION, len(body), req_id, opcode)
        # struct format above produces 11 bytes (B+B+I+I+B); the space in the
        # format string is ignored by struct.
        self._sock.sendall(header + body)

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise BinmonError("socket closed by emulator")
            buf.extend(chunk)
        return bytes(buf)

    def _read_response(self) -> BinmonResponse:
        head = self._recv_exact(RESP_HEADER_LEN)
        stx, ver, body_len, opcode, err, req_id = struct.unpack("<BBIBBI", head)
        if stx != STX or ver != API_VERSION:
            raise BinmonError(f"bad framing: stx={stx:#x} ver={ver:#x}")
        body = self._recv_exact(body_len) if body_len else b""
        return BinmonResponse(opcode=opcode, err=err, req_id=req_id, body=body)

    def _drain_unsolicited(self, max_wait: float) -> None:
        assert self._sock is not None
        deadline = time.monotonic() + max_wait
        old_to = self._sock.gettimeout()
        try:
            while time.monotonic() < deadline:
                self._sock.settimeout(max(0.05, deadline - time.monotonic()))
                try:
                    resp = self._read_response()
                except socket.timeout:
                    return
                if self.on_event is not None and resp.req_id == 0xFFFFFFFF:
                    try:
                        self.on_event(resp)
                    except Exception:  # noqa: BLE001
                        log.exception("on_event raised")
        finally:
            self._sock.settimeout(old_to)

    # ---- public call interface -----------------------------------------

    def call(
        self,
        opcode: int,
        body: bytes = b"",
        require_ok: bool = True,
        timeout: float = 5.0,
    ) -> BinmonResponse:
        return self._call_inner(opcode, body, require_ok, timeout, auto_resume=self.auto_resume)

    def call_keep_halted(
        self,
        opcode: int,
        body: bytes = b"",
        require_ok: bool = True,
        timeout: float = 5.0,
    ) -> BinmonResponse:
        """Like call() but never sends a follow-up EXIT, regardless of
        auto_resume. Use for the initial post-connect handshake or for
        scripted breakpoint sequences."""
        return self._call_inner(opcode, body, require_ok, timeout, auto_resume=False)

    def halted(self):
        """Context manager: temporarily disable auto-resume so subsequent
        mem_set / registers_set calls leave the CPU halted between them.

        Required when scripting a multi-step inject-and-run flow, e.g.

            bm.run_until_pc(LOOP_TOP)        # CPU halts here
            with bm.halted():
                bm.mem_set(0x0E44, b"\\x30")  # would otherwise resume CPU
                bm.registers_set({0: 0x30})
                bm.run_until_pc(LOOP_TOP)    # explicit resume + wait

        Without this, every mem_set call sends an EXIT after the operation,
        so the CPU runs for a few microseconds between each setup step
        (long enough for an IRQ to fire and for the keyboard scanner to
        rewrite $0E44 back to $FF, breaking the injection)."""
        from contextlib import contextmanager

        @contextmanager
        def _hold():
            saved = self.auto_resume
            self.auto_resume = False
            try:
                yield
            finally:
                self.auto_resume = saved

        return _hold()

    def _call_inner(
        self,
        opcode: int,
        body: bytes,
        require_ok: bool,
        timeout: float,
        auto_resume: bool,
    ) -> BinmonResponse:
        with self._lock:
            assert self._sock is not None, "call connect() first"
            req_id = self._next_req()
            old_to = self._sock.gettimeout()
            self._sock.settimeout(timeout)
            try:
                self._send(opcode, body, req_id)
                deadline = time.monotonic() + timeout
                while True:
                    if time.monotonic() > deadline:
                        raise BinmonError(f"timeout waiting for response to opcode {opcode:#x}")
                    resp = self._read_response()
                    if resp.req_id == req_id:
                        if require_ok and resp.err != ERR_OK:
                            raise BinmonError(f"opcode {opcode:#x} returned err {resp.err:#x}")
                        # Send EXIT (CPU resume) after each command unless this
                        # call was itself EXIT (avoid infinite recursion) or the
                        # caller asked to stay halted.
                        if auto_resume and opcode != OPCODE.EXIT:
                            exit_req = self._next_req()
                            self._send(OPCODE.EXIT, b"", exit_req)
                            # Drain the EXIT response (and any RESUMED event).
                            ex_deadline = time.monotonic() + timeout
                            while time.monotonic() < ex_deadline:
                                ex = self._read_response()
                                if ex.req_id == exit_req:
                                    break
                                if self.on_event is not None and ex.req_id == 0xFFFFFFFF:
                                    try:
                                        self.on_event(ex)
                                    except Exception:  # noqa: BLE001
                                        log.exception("on_event raised")
                        return resp
                    if self.on_event is not None and resp.req_id == 0xFFFFFFFF:
                        try:
                            self.on_event(resp)
                        except Exception:  # noqa: BLE001
                            log.exception("on_event raised")
            finally:
                self._sock.settimeout(old_to)

    # ---- wrappers ------------------------------------------------------

    def exit(self) -> None:
        """Resume the CPU (ack the initial STOPPED that binmon connect emits)."""
        self.call(OPCODE.EXIT)

    def reset(self, mode: int = 0) -> None:
        """0=soft reset, 1=hard reset; 8/9/10/11 = drive resets."""
        self.call(OPCODE.RESET, bytes([mode]), require_ok=False)

    def ping(self) -> None:
        self.call(OPCODE.PING)

    def mem_get(
        self,
        start: int,
        end: int,
        side_effects: bool = False,
        memspace: int = 0,
        bank: int = 0,
    ) -> bytes:
        body = struct.pack("<BHHBH", int(side_effects), start, end, memspace, bank)
        resp = self.call(OPCODE.MEM_GET, body)
        if len(resp.body) < 2:
            raise BinmonError("mem_get short response")
        n = struct.unpack("<H", resp.body[:2])[0]
        return resp.body[2 : 2 + n]

    def mem_set(
        self,
        start: int,
        data: bytes,
        side_effects: bool = False,
        memspace: int = 0,
        bank: int = 0,
    ) -> None:
        """Write data into memory at [start, start+len(data))."""
        if not data:
            return
        end = start + len(data) - 1
        body = struct.pack("<BHHBH", int(side_effects), start, end, memspace, bank) + bytes(data)
        self.call(OPCODE.MEM_SET, body)

    # ---- CPU registers (REGISTERS_GET 0x31 / REGISTERS_SET 0x32) ------
    #
    # VICE register IDs (monitor.h): 0=A, 1=X, 2=Y, 3=PC, 4=SP, 5=FLAGS.
    # Each register entry in the protocol is `size(u8) id(u8) value(u16 LE)`;
    # size is the byte count of (id + value) — always 3 for these regs.

    def registers_get(self, memspace: int = 0) -> dict[int, int]:
        """Return all registers as {id: value}."""
        resp = self.call(OPCODE.REGISTERS_GET, struct.pack("<B", memspace))
        if len(resp.body) < 2:
            raise BinmonError("registers_get short response")
        n = struct.unpack("<H", resp.body[:2])[0]
        out: dict[int, int] = {}
        p = 2
        for _ in range(n):
            sz = resp.body[p]
            rid = resp.body[p + 1]
            val = struct.unpack("<H", resp.body[p + 2 : p + 4])[0]
            out[rid] = val
            p += 1 + sz
        return out

    def registers_set(self, regs: dict[int, int], memspace: int = 0) -> None:
        """Set named registers. regs: {id: value}."""
        items = list(regs.items())
        body = struct.pack("<BH", memspace, len(items))
        for rid, val in items:
            body += struct.pack("<BBH", 3, rid, val & 0xFFFF)
        self.call(OPCODE.REGISTERS_SET, body)

    # ---- single-shot execution helpers --------------------------------

    def advance_instructions(self, count: int = 1, step_over_subroutines: bool = False) -> None:
        """ADVANCE_INSTRUCTIONS (0x71). Steps the CPU by `count` instructions
        while keeping it halted (no auto-resume)."""
        body = struct.pack("<BH", int(step_over_subroutines), count)
        self._call_inner(
            OPCODE.ADVANCE_INSTRUCTIONS,
            body,
            require_ok=True,
            timeout=5.0,
            auto_resume=False,
        )

    def run_until_pc(self, target: int, timeout: float = 5.0) -> None:
        """Resume CPU until execution reaches `target`. Implementation: set a
        temporary stop-on-hit checkpoint at target, EXIT, wait for the
        unsolicited CHECKPOINT_INFO with hit=True, then delete the checkpoint.

        Use after registers_set(PC=...) to "run one routine to completion".
        Caller is responsible for arranging that PC will actually reach
        target (typically: push fake return address, set PC to routine,
        target = the dummy return-to-self instruction).

        Always installs the checkpoint without auto-resuming, so behaviour
        is identical whether or not the caller is inside `bm.halted()`.

        Fast-path: if the CPU is already halted at `target`, return
        immediately without installing a checkpoint or resuming."""
        # Fast-path: PC already at target (e.g. caller's previous
        # run_until_pc() left us here). Avoids installing a checkpoint
        # and waiting on an event that will never fire because CPU never
        # leaves target.
        try:
            regs_now = self._call_inner(
                OPCODE.REGISTERS_GET,
                struct.pack("<B", MEMSPACE_MAIN),
                require_ok=True,
                timeout=timeout,
                auto_resume=False,
            )
            # Parse PC out of the response (same shape as registers_get).
            if len(regs_now.body) >= 2:
                n = struct.unpack("<H", regs_now.body[:2])[0]
                p = 2
                for _ in range(n):
                    sz = regs_now.body[p]
                    rid = regs_now.body[p + 1]
                    val = struct.unpack("<H", regs_now.body[p + 2 : p + 4])[0]
                    if rid == 3 and val == target:
                        return
                    p += 1 + sz
        except BinmonError:
            pass

        # Install the checkpoint without resuming CPU — otherwise the
        # auto_resume EXIT can race the manual EXIT below and the CPU may
        # be already past target before we start watching for events.
        body = struct.pack(
            "<HHBBBB B",
            target & 0xFFFF,
            target & 0xFFFF,
            1,
            1,
            CHECK_EXEC,
            1,  # stop_when_hit, enabled, op, temporary
            MEMSPACE_MAIN,
        )
        resp = self._call_inner(
            OPCODE.CHECKPOINT_SET,
            body,
            require_ok=True,
            timeout=timeout,
            auto_resume=False,
        )
        cp = self._parse_checkpoint_info(resp.body)
        try:
            # Resume CPU. We send EXIT manually and wait for the unsolicited
            # STOPPED event to indicate the checkpoint fired.
            with self._lock:
                assert self._sock is not None
                old_to = self._sock.gettimeout()
                self._sock.settimeout(timeout)
                try:
                    exit_req = self._next_req()
                    self._send(OPCODE.EXIT, b"", exit_req)
                    deadline = time.monotonic() + timeout
                    exit_acked = False
                    hit = False
                    while time.monotonic() < deadline:
                        resp = self._read_response()
                        if resp.req_id == exit_req:
                            exit_acked = True
                            continue
                        if resp.req_id == 0xFFFFFFFF:
                            if resp.opcode == 0x11:  # CHECKPOINT_INFO event
                                if len(resp.body) >= 5 and resp.body[4]:
                                    hit = True
                            elif resp.opcode == OPCODE.STOPPED:
                                # Server emits STOPPED whenever the CPU
                                # halts. Return as soon as we've seen a
                                # checkpoint hit (which arrives moments
                                # before the STOPPED event).
                                if hit:
                                    return
                    raise BinmonError(
                        f"run_until_pc(${target:04X}) timed out "
                        f"(exit_acked={exit_acked}, hit={hit})"
                    )
                finally:
                    self._sock.settimeout(old_to)
        finally:
            # Make sure we don't leave the temp checkpoint behind on error
            # paths. CHECKPOINT_DELETE is harmless if VICE already auto-
            # removed it (temporary=True), but safer to be explicit.
            # Use call_keep_halted so we don't unintentionally resume the
            # CPU after a successful halt at target.
            try:
                self._call_inner(
                    OPCODE.CHECKPOINT_DELETE,
                    struct.pack("<I", cp.checknum),
                    require_ok=False,
                    timeout=2.0,
                    auto_resume=False,
                )
            except BinmonError:
                pass

    def autostart(self, path: bytes | str, run_after: bool = True, file_index: int = 0) -> None:
        """Autostart a disk/PRG image at runtime. path may be PETSCII bytes
        or a UTF-8 str (will be encoded to ASCII)."""
        if isinstance(path, str):
            path = path.encode("ascii")
        body = struct.pack("<BHB", int(run_after), file_index, len(path)) + path
        self.call(OPCODE.AUTOSTART, body)

    # ---- resources -----------------------------------------------------

    def resource_set_string(self, name: str, value: str) -> None:
        """RESOURCE_SET (0x44) for a string-typed resource. Used to attach
        and detach disk images at runtime by setting Drive8Image, etc."""
        name_b = name.encode("ascii")
        value_b = value.encode("ascii")
        body = (
            bytes([0x00])  # type 0 = string
            + bytes([len(name_b)])
            + name_b  # name with u8 length
            + bytes([len(value_b)])
            + value_b  # value with u8 length
        )
        self.call(OPCODE.RESOURCE_SET, body)

    def resource_set_int(self, name: str, value: int) -> None:
        """RESOURCE_SET (0x44) for an int-typed resource."""
        name_b = name.encode("ascii")
        body = (
            bytes([0x01])  # type 1 = int
            + bytes([len(name_b)])
            + name_b
            + bytes([4])  # value length is 4 (u32 LE)
            + struct.pack("<I", value & 0xFFFFFFFF)
        )
        self.call(OPCODE.RESOURCE_SET, body)

    # ---- DRIVE_ATTACH (asid-vice 0x78) --------------------------------
    #
    # Stock VICE binmon has no clean attach/detach primitive — the disk
    # image isn't bound to a resource, so RESOURCE_SET cannot reach it,
    # and the RESOURCE_SET handler also rejects zero-length values. The
    # asid-vice fork adds opcode 0x78 specifically for this. See the
    # 'DRIVE_ATTACH' section of the asid-vice README.

    def attach_drive(self, path: str, unit: int = 8, drive: int = 0) -> None:
        """Attach a host-side disk image to (unit, drive). VICE detaches
        any previously-attached image first, which closes open files and
        flushes the BAM back to host disk — so a same-path call is also
        the canonical way to flush after a guest-side save."""
        path_b = path.encode("ascii")
        if len(path_b) > 255:
            raise BinmonError("path too long (255 bytes max)")
        body = bytes([unit, drive, len(path_b)]) + path_b
        self.call(OPCODE.DRIVE_ATTACH, body)

    def detach_drive(self, unit: int = 8, drive: int = 0) -> None:
        """Detach whatever image is currently on (unit, drive)."""
        body = bytes([unit, drive, 0])
        self.call(OPCODE.DRIVE_ATTACH, body)

    def flush_drive(self, path: str, unit: int = 8, drive: int = 0) -> None:
        """Force a flush of drive (unit, drive)'s in-memory state to the
        host-side .d64 file at `path`. Implementation: re-attach the
        same path. VICE's attach code detaches the existing image first,
        which closes any open files and writes the BAM/dirent state
        back to disk before the same image is re-opened.

        Also a useful hook for an external observer wanting a consistent
        snapshot of the disk: between the implicit detach and the
        re-attach no writer owns the file, so a `shutil.copy()` taken
        in that window is guaranteed safe."""
        self.attach_drive(path, unit=unit, drive=drive)

    # ---- keymatrix (asid-vice) -----------------------------------------

    def keymatrix_set(self, keys: Iterable[tuple[int, int, int]]) -> None:
        """Sticky bit-set. keys = [(row, col, value), ...] (value 0/1).
        row may be a negative custom-key sentinel (e.g. -3 for RESTORE)."""
        keys = list(keys)
        body = bytes([len(keys)]) + b"".join(struct.pack("<bbB", r, c, v) for r, c, v in keys)
        self.call(OPCODE.KEYMATRIX_SET, body)

    def keymatrix_release_all(self) -> None:
        """Hard clear of the entire matrix (passes count=0)."""
        # The text command 'keymatrix release' with no args clears all bits;
        # the binmon equivalent is KEYMATRIX_SET with count=0 which the
        # server interprets as "no changes" — so we set every bit explicitly.
        keys = [(r, c, 0) for r in range(8) for c in range(8)]
        self.keymatrix_set(keys)

    def keymatrix_tap(
        self,
        keys: Iterable[tuple[int, int]],
        mode: int = TAP_MODE_OBSERVED,
        frames: int = 60,
    ) -> None:
        """Tap a chord. Returns immediately — call keymatrix_get() to read result."""
        keys = list(keys)
        body = struct.pack("<BHB", mode, frames, len(keys)) + b"".join(
            struct.pack("<bb", r, c) for r, c in keys
        )
        self.call(OPCODE.KEYMATRIX_TAP, body)

    def keymatrix_get(self) -> TapResult:
        resp = self.call(OPCODE.KEYMATRIX_GET)
        if len(resp.body) < 24:
            raise BinmonError(f"keymatrix_get short response ({len(resp.body)} bytes)")
        b = resp.body
        return TapResult(
            keyarr=b[0:8],
            custom=b[8],
            cia1_reads_total=struct.unpack("<I", b[12:16])[0],
            cia1_reads_sampling=struct.unpack("<I", b[16:20])[0],
            release_reason=b[20],
            n_keys=b[21],
            frames_until_timeout=struct.unpack("<H", b[22:24])[0],
        )

    def screen_get(self) -> bytes:
        """Return the raw 4072-byte SCREEN_GET response body."""
        resp = self.call(OPCODE.SCREEN_GET)
        return resp.body

    # ---- checkpoints (CHECKPOINT_*, opcodes 0x11..0x15) ----------------

    @staticmethod
    def _parse_checkpoint_info(body: bytes) -> Checkpoint:
        if len(body) < 23:
            raise BinmonError(f"checkpoint info short response ({len(body)} bytes)")
        (
            checknum,
            hit,
            start,
            end,
            stop,
            enabled,
            op,
            temp,
            hit_count,
            ignore_count,
            has_cond,
            memspace,
        ) = struct.unpack("<I B HH BBBB II BB", body[:23])
        return Checkpoint(
            checknum=checknum,
            hit=bool(hit),
            start=start,
            end=end,
            stop_when_hit=bool(stop),
            enabled=bool(enabled),
            op=op,
            temporary=bool(temp),
            hit_count=hit_count,
            ignore_count=ignore_count,
            has_condition=bool(has_cond),
            memspace=memspace,
        )

    def checkpoint_set(
        self,
        start: int,
        end: int | None = None,
        op: int = CHECK_EXEC,
        stop_when_hit: bool = False,
        enabled: bool = True,
        temporary: bool = False,
        memspace: int = MEMSPACE_MAIN,
        silent: bool = False,
    ) -> Checkpoint:
        """Add a checkpoint over [start, end] (inclusive). Defaults to a
        non-stopping exec-only watchpoint suitable for live PC tracing.

        stop_when_hit=False keeps the CPU running through the hit. The
        hit is reported as an unsolicited CHECKPOINT_INFO event with
        req_id=0xFFFFFFFF and hit=True; route on_event to capture them.

        silent=True (asid-vice extension) suppresses the per-hit
        CHECKPOINT_INFO event AND VICE-side trace-print/disassemble
        work. hit_count still increments and is readable via
        checkpoint_list, so this is the right mode for byte-granular
        polled coverage where the harness only needs the cumulative
        hit set and does not want to drain ~10^7 events/s during warp
        playback."""
        if end is None:
            end = start
        body = struct.pack(
            "<HHBBBB BB",
            start & 0xFFFF,
            end & 0xFFFF,
            int(stop_when_hit),
            int(enabled),
            op & 0xFF,
            int(temporary),
            memspace & 0xFF,
            int(silent),
        )
        resp = self.call(OPCODE.CHECKPOINT_SET, body)
        return self._parse_checkpoint_info(resp.body)

    def checkpoint_get(self, checknum: int) -> Checkpoint:
        resp = self.call(OPCODE.CHECKPOINT_GET, struct.pack("<I", checknum))
        return self._parse_checkpoint_info(resp.body)

    def checkpoint_delete(self, checknum: int) -> None:
        self.call(OPCODE.CHECKPOINT_DELETE, struct.pack("<I", checknum))

    def checkpoint_toggle(self, checknum: int, enabled: bool) -> None:
        self.call(OPCODE.CHECKPOINT_TOGGLE, struct.pack("<IB", checknum, int(enabled)))

    def checkpoint_list(self) -> list[Checkpoint]:
        """Issue CHECKPOINT_LIST. The server replies with one
        CHECKPOINT_INFO per existing checkpoint and ONE final
        CHECKPOINT_LIST response. We collect the infos that share our
        request_id, then accept the LIST as the terminator."""
        with self._lock:
            assert self._sock is not None, "call connect() first"
            req_id = self._next_req()
            self._send(OPCODE.CHECKPOINT_LIST, b"", req_id)
            checkpoints: list[Checkpoint] = []
            old_to = self._sock.gettimeout()
            self._sock.settimeout(5.0)
            try:
                while True:
                    resp = self._read_response()
                    if resp.req_id != req_id:
                        if self.on_event is not None and resp.req_id == 0xFFFFFFFF:
                            try:
                                self.on_event(resp)
                            except Exception:  # noqa: BLE001
                                log.exception("on_event raised")
                        continue
                    if resp.opcode == 0x11:  # CHECKPOINT_INFO
                        checkpoints.append(self._parse_checkpoint_info(resp.body))
                        continue
                    if resp.opcode == 0x14:  # CHECKPOINT_LIST terminator
                        if self.auto_resume:
                            exit_req = self._next_req()
                            self._send(OPCODE.EXIT, b"", exit_req)
                            while True:
                                ex = self._read_response()
                                if ex.req_id == exit_req:
                                    break
                        return checkpoints
                    raise BinmonError(f"unexpected opcode {resp.opcode:#x} in checkpoint_list")
            finally:
                self._sock.settimeout(old_to)

    # ---- cpuhistory (CPUHISTORY_GET, opcode 0x86) ----------------------

    def cpuhistory_get(
        self, count: int = 256, memspace: int = MEMSPACE_MAIN
    ) -> list[CpuHistoryRecord]:
        """Return the last `count` instructions executed in `memspace`.

        Body layout per monitor_binary_process_cpuhistory():
          [0]    memspace (u8)
          [1..4] count (u32 LE — server truncates to u16)

        Response body:
          [0..3] N records (u32 LE)
          then N * (1 byte item_size + item_size payload):
            payload[0..1]   register_block_count (u16 LE)
            payload[2..]    each register: 1 byte item_size, 1 byte id,
                            2 bytes LE value
            (after register block) 8 bytes cycle (u64 LE), 1 byte
            instruction_length (=4), then op, p1, p2, 0xff placeholder.
        """
        if count < 1 or count > 0xFFFF:
            raise BinmonError(f"cpuhistory count out of range: {count}")
        body = struct.pack("<BI", memspace & 0xFF, count)
        resp = self.call(OPCODE.CPUHISTORY_GET, body, timeout=10.0)
        if len(resp.body) < 4:
            raise BinmonError("cpuhistory short response")
        n = struct.unpack("<I", resp.body[:4])[0]
        out: list[CpuHistoryRecord] = []
        cur = 4
        for _ in range(n):
            if cur >= len(resp.body):
                raise BinmonError("cpuhistory truncated mid-record")
            item_size = resp.body[cur]
            cur += 1
            payload = resp.body[cur : cur + item_size]
            if len(payload) < item_size:
                raise BinmonError("cpuhistory payload short")
            cur += item_size

            # Register block.
            reg_count = struct.unpack("<H", payload[:2])[0]
            p = 2
            registers: dict[int, int] = {}
            for _r in range(reg_count):
                if p + 4 > len(payload):
                    raise BinmonError("cpuhistory register block short")
                rsz = payload[p]
                rid = payload[p + 1]
                rval = struct.unpack("<H", payload[p + 2 : p + 4])[0]
                registers[rid] = rval
                p += 1 + rsz  # rsz already includes id+value
            # cycle (u64 LE)
            if p + 8 > len(payload):
                raise BinmonError("cpuhistory cycle field short")
            cycle = struct.unpack("<Q", payload[p : p + 8])[0]
            p += 8
            # instruction_length + bytes
            if p + 1 > len(payload):
                raise BinmonError("cpuhistory inst_len missing")
            inst_len = payload[p]
            p += 1
            if p + inst_len > len(payload):
                raise BinmonError("cpuhistory inst bytes short")
            op_b = payload[p] if inst_len > 0 else 0
            p1 = payload[p + 1] if inst_len > 1 else 0
            p2 = payload[p + 2] if inst_len > 2 else 0
            # payload[p+3] = 0xff placeholder (third operand for non-6502)
            out.append(
                CpuHistoryRecord(
                    cycle=cycle,
                    op=op_b,
                    p1=p1,
                    p2=p2,
                    registers=registers,
                )
            )
        return out
