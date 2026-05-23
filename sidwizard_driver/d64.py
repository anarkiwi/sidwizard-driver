"""Minimal Commodore 1541 .d64 disk image writer (single PRG)."""

from __future__ import annotations

SECTOR_SIZE = 256
TOTAL_SECTORS = 683
D64_SIZE = SECTOR_SIZE * TOTAL_SECTORS  # 174848

SECTORS_PER_TRACK: list[int] = (
    [21] * 17  # tracks 1..17
    + [19] * 7  # tracks 18..24
    + [18] * 6  # tracks 25..30
    + [17] * 5  # tracks 31..35
)

DIR_TRACK = 18
BAM_SECTOR = 0
FIRST_DIR_SECTOR = 1

SECTOR_DATA = SECTOR_SIZE - 2

PRG_START_TRACK = 17
PRG_START_SECTOR = 0


def _track_sector_offset(track: int, sector: int) -> int:
    if not 1 <= track <= 35:
        raise ValueError(f"track out of range: {track}")
    spt = SECTORS_PER_TRACK[track - 1]
    if not 0 <= sector < spt:
        raise ValueError(f"sector {sector} out of range for track {track} (0..{spt - 1})")
    off = 0
    for t in range(1, track):
        off += SECTORS_PER_TRACK[t - 1]
    return (off + sector) * SECTOR_SIZE


def _petscii_name(name: str, fill: int = 0xA0, length: int = 16) -> bytes:
    up = name.upper()
    if not all(0x20 <= ord(c) < 0x7F for c in up):
        raise ValueError(f"filename has non-printable ASCII: {name!r}")
    raw = up.encode("ascii")
    if len(raw) > length:
        raise ValueError(f"filename too long (>{length} chars): {name!r}")
    return raw + bytes([fill] * (length - len(raw)))


def build_d64_with_prg(
    prg_bytes: bytes,
    filename: str,
    disk_name: str = "SIDWIZARD",
    disk_id: bytes = b"01",
) -> bytes:
    """Return a 174848-byte d64 image containing one PRG file."""
    if len(prg_bytes) == 0:
        raise ValueError("prg_bytes is empty")
    if len(disk_id) != 2:
        raise ValueError("disk_id must be exactly 2 bytes")

    image = bytearray(D64_SIZE)

    needed_sectors = (len(prg_bytes) + SECTOR_DATA - 1) // SECTOR_DATA
    spt_17 = SECTORS_PER_TRACK[PRG_START_TRACK - 1]
    if needed_sectors > spt_17:
        # Refuse rather than silently implementing a multi-track chain
        # we can't test offline; SWM files in practice fit one track.
        raise ValueError(
            f"prg too large for single-track chain on track {PRG_START_TRACK}: "
            f"{needed_sectors} sectors needed, {spt_17} available"
        )

    cursor = 0
    used_sectors_on_prg_track: list[int] = []
    for sector_index in range(needed_sectors):
        sector_no = (PRG_START_SECTOR + sector_index) % spt_17
        used_sectors_on_prg_track.append(sector_no)
        is_last = sector_index == needed_sectors - 1
        chunk = prg_bytes[cursor : cursor + SECTOR_DATA]
        cursor += len(chunk)
        sector_buf = bytearray(SECTOR_SIZE)
        if is_last:
            sector_buf[0] = 0
            sector_buf[1] = len(chunk) + 1
        else:
            next_sector_no = (PRG_START_SECTOR + sector_index + 1) % spt_17
            sector_buf[0] = PRG_START_TRACK
            sector_buf[1] = next_sector_no
        sector_buf[2 : 2 + len(chunk)] = chunk
        off = _track_sector_offset(PRG_START_TRACK, sector_no)
        image[off : off + SECTOR_SIZE] = sector_buf

    dir_off = _track_sector_offset(DIR_TRACK, FIRST_DIR_SECTOR)
    dir_sector = bytearray(SECTOR_SIZE)
    dir_sector[0] = 0
    dir_sector[1] = 0xFF
    entry = dir_sector
    entry[2] = 0x82  # closed PRG
    entry[3] = PRG_START_TRACK
    entry[4] = PRG_START_SECTOR
    entry[5:21] = _petscii_name(filename, fill=0xA0, length=16)
    entry[28:30] = b"\x00\x00"
    size_blocks = needed_sectors
    entry[30] = size_blocks & 0xFF
    entry[31] = (size_blocks >> 8) & 0xFF

    image[dir_off : dir_off + SECTOR_SIZE] = dir_sector

    bam_off = _track_sector_offset(DIR_TRACK, BAM_SECTOR)
    bam_sector = bytearray(SECTOR_SIZE)
    bam_sector[0] = DIR_TRACK
    bam_sector[1] = FIRST_DIR_SECTOR
    bam_sector[2] = 0x41  # DOS 'A'
    bam_sector[3] = 0x00

    for track in range(1, 36):
        spt = SECTORS_PER_TRACK[track - 1]
        if track == DIR_TRACK:
            free = 0
            bitmap = 0
        elif track == PRG_START_TRACK:
            free = spt - len(used_sectors_on_prg_track)
            bitmap = (1 << spt) - 1
            for s in used_sectors_on_prg_track:
                bitmap &= ~(1 << s)
        else:
            free = spt
            bitmap = (1 << spt) - 1
        slot = 4 + (track - 1) * 4
        bam_sector[slot] = free
        bam_sector[slot + 1] = bitmap & 0xFF
        bam_sector[slot + 2] = (bitmap >> 8) & 0xFF
        bam_sector[slot + 3] = (bitmap >> 16) & 0xFF

    bam_sector[0x90:0xA0] = _petscii_name(disk_name, fill=0xA0, length=16)
    bam_sector[0xA0] = 0xA0
    bam_sector[0xA1] = 0xA0
    bam_sector[0xA2:0xA4] = disk_id
    bam_sector[0xA4] = 0xA0
    bam_sector[0xA5] = 0x32
    bam_sector[0xA6] = 0x41
    bam_sector[0xA7:0xAB] = b"\xa0\xa0\xa0\xa0"

    image[bam_off : bam_off + SECTOR_SIZE] = bam_sector

    return bytes(image)


def write_d64_with_prg(out_path: str, prg_bytes: bytes, filename: str) -> None:
    image = build_d64_with_prg(prg_bytes, filename)
    with open(out_path, "wb") as fp:
        fp.write(image)


def write_d64_with_swm(out_path: str, swm_path: str, filename: str | None = None) -> str:
    """Copy an on-disk ``.swm`` into a fresh single-file d64.

    SID-Wizard's ``regname`` appends ``.SWM`` to the typed filename, so
    the on-disk name must carry the extension too. Returns the on-disk
    filename (with extension) so the caller can drive the file dialog.
    """
    with open(swm_path, "rb") as fp:
        swm_bytes = fp.read()
    if filename is None:
        import os

        stem = os.path.splitext(os.path.basename(swm_path))[0].upper()
        filename = stem + ".SWM"
    write_d64_with_prg(out_path, swm_bytes, filename)
    return filename


__all__ = [
    "D64_SIZE",
    "SECTOR_SIZE",
    "TOTAL_SECTORS",
    "SECTORS_PER_TRACK",
    "build_d64_with_prg",
    "write_d64_with_prg",
    "write_d64_with_swm",
]
