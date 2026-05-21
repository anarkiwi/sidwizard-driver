"""Minimal Commodore 1541 .d64 disk image writer.

What this models
----------------
Just enough of the d64 format to make a fresh 35-track image holding a
single PRG file. SID-Wizard's loader reads ``.swm`` files via the
KERNAL load path; if we hand the editor a one-file disk, navigating
the file selector to the first entry and pressing RETURN loads our
tune through the editor's own (correct) ``loadtun`` path — including
the in-place depack we'd otherwise have to drive manually.

Scope
-----
* 35-track, 683-sector standard d64 (174848 bytes).
* One PRG file, written as a single linear sector chain on track 17.
* Track 18 = directory + BAM, with all other tracks free.

Not modelled
------------
* Multiple files per image (not needed; we ship one tune at a time).
* GEOS / extended formats / error info bytes.
* 40-track variants (overkill).

References
----------
* The d64 format is documented widely; canonical reference is
  ``https://vice-emu.sourceforge.io/vice_17.html`` and the format
  description in VICE's ``c1541``.
"""

from __future__ import annotations

# Total image size and geometry.
SECTOR_SIZE = 256
TOTAL_SECTORS = 683
D64_SIZE = SECTOR_SIZE * TOTAL_SECTORS  # 174848

# Sectors per track, indexed [track-1]. tracks 1..17=21, 18..24=19,
# 25..30=18, 31..35=17. Track 0 is reserved (no such track).
SECTORS_PER_TRACK: list[int] = (
    [21] * 17  # tracks 1..17
    + [19] * 7  # tracks 18..24
    + [18] * 6  # tracks 25..30
    + [17] * 5  # tracks 31..35
)

DIR_TRACK = 18
BAM_SECTOR = 0
FIRST_DIR_SECTOR = 1

# The data sectors per file occupy 254 bytes; the first two bytes of
# each sector are the (next_track, next_sector) link. The last sector
# in a chain has next_track=0 and next_sector=(bytes_used_in_sector - 1).
SECTOR_DATA = SECTOR_SIZE - 2

# Where we put our single PRG. Track 17 is the last data track before
# the directory, conventional for tools that wrote "data after" the
# directory. Any track other than 18 would work.
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
    """Encode an ASCII filename as PETSCII, pad with $A0 to ``length``.

    Lowercase is folded to uppercase (PETSCII upper/graphics charset);
    non-ASCII characters raise. d64 filenames are uppercase PETSCII
    and 16 bytes long with $A0 padding (shifted-space).
    """
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
    """Return a 174848-byte d64 image containing one PRG file.

    ``prg_bytes`` is the raw PRG payload INCLUDING its 2-byte
    little-endian load address — for SWM files this matches the bytes
    on disk in pysidwizard's tests/data directory.

    ``filename`` is the on-disk name (max 16 chars, ASCII, folded
    upper-case). The file is written as type PRG.
    """
    if len(prg_bytes) == 0:
        raise ValueError("prg_bytes is empty")
    if len(disk_id) != 2:
        raise ValueError("disk_id must be exactly 2 bytes")

    image = bytearray(D64_SIZE)

    # ---- write the PRG sector chain ----
    needed_sectors = (len(prg_bytes) + SECTOR_DATA - 1) // SECTOR_DATA
    spt_17 = SECTORS_PER_TRACK[PRG_START_TRACK - 1]
    if needed_sectors > spt_17:
        # Could spill to additional tracks; not required for typical
        # SWM sizes (largest in pysidwizard's samples is ~8 KiB = 32
        # sectors, well under 21). Refuse rather than silently
        # implementing a multi-track chain we can't test offline.
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
            sector_buf[0] = 0  # track 0 = end of chain
            # next_sector field holds (bytes_used_in_this_sector - 1 + 1)
            # = number of data bytes + 1, since the file pointer is
            # past the last data byte. d64 spec: "the last byte used in
            # this sector + 1" — yields len(chunk)+1.
            sector_buf[1] = len(chunk) + 1
        else:
            next_sector_no = (PRG_START_SECTOR + sector_index + 1) % spt_17
            sector_buf[0] = PRG_START_TRACK
            sector_buf[1] = next_sector_no
        sector_buf[2 : 2 + len(chunk)] = chunk
        off = _track_sector_offset(PRG_START_TRACK, sector_no)
        image[off : off + SECTOR_SIZE] = sector_buf

    # ---- write the directory entry (track 18, sector 1) ----
    dir_off = _track_sector_offset(DIR_TRACK, FIRST_DIR_SECTOR)
    dir_sector = bytearray(SECTOR_SIZE)
    dir_sector[0] = 0  # no next dir sector (only one entry)
    dir_sector[1] = 0xFF  # all bytes in this sector are valid
    # File entry at offset 2..31 (8 entries per directory sector;
    # we only fill the first).
    entry = dir_sector  # writing into the dir_sector at offset 2
    entry[2] = 0x82  # file type: closed PRG (bit 7 = closed, low nybble 2 = PRG)
    entry[3] = PRG_START_TRACK
    entry[4] = PRG_START_SECTOR
    entry[5:21] = _petscii_name(filename, fill=0xA0, length=16)
    # entry[21..25] = REL track/sector/length; zero for PRG
    # entry[26..27] = REL side-sector; zero
    # entry[28..29] = unused
    entry[28:30] = b"\x00\x00"
    # entry[30..31] = file size in blocks, little-endian
    size_blocks = needed_sectors
    entry[30] = size_blocks & 0xFF
    entry[31] = (size_blocks >> 8) & 0xFF

    image[dir_off : dir_off + SECTOR_SIZE] = dir_sector

    # ---- write the BAM (track 18, sector 0) ----
    bam_off = _track_sector_offset(DIR_TRACK, BAM_SECTOR)
    bam_sector = bytearray(SECTOR_SIZE)
    bam_sector[0] = DIR_TRACK  # link to first directory sector
    bam_sector[1] = FIRST_DIR_SECTOR
    bam_sector[2] = 0x41  # DOS version: 'A' (CBM DOS 2.6)
    bam_sector[3] = 0x00  # unused / reserved

    # Per-track BAM entries: 4 bytes each, tracks 1..35 at offsets 4..143.
    # First byte = free sector count; next 3 bytes = bitmap (LSB = sector 0).
    for track in range(1, 36):
        spt = SECTORS_PER_TRACK[track - 1]
        if track == DIR_TRACK:
            free = 0
            bitmap = 0
        elif track == PRG_START_TRACK:
            # All except the ones we used.
            free = spt - len(used_sectors_on_prg_track)
            bitmap = (1 << spt) - 1  # all available
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

    # Disk name (PETSCII, padded with $A0) at $90..$9F (16 bytes).
    bam_sector[0x90:0xA0] = _petscii_name(disk_name, fill=0xA0, length=16)
    bam_sector[0xA0] = 0xA0
    bam_sector[0xA1] = 0xA0
    bam_sector[0xA2:0xA4] = disk_id
    bam_sector[0xA4] = 0xA0  # filler
    bam_sector[0xA5] = 0x32  # DOS version: '2'
    bam_sector[0xA6] = 0x41  # DOS format: 'A'
    bam_sector[0xA7:0xAB] = b"\xa0\xa0\xa0\xa0"
    # Rest of BAM sector (0xAB..0xFF) stays zero — fine for c1541's
    # tolerance; the byte $A0 padding rule applies only to the disk
    # name and 2-char ID region.

    image[bam_off : bam_off + SECTOR_SIZE] = bam_sector

    return bytes(image)


def write_d64_with_prg(out_path: str, prg_bytes: bytes, filename: str) -> None:
    """Write a fresh d64 image to ``out_path`` containing one PRG."""
    image = build_d64_with_prg(prg_bytes, filename)
    with open(out_path, "wb") as fp:
        fp.write(image)


def write_d64_with_swm(out_path: str, swm_path: str, filename: str | None = None) -> str:
    """Convenience: copy an on-disk ``.swm`` into a fresh single-file d64.

    The SWM file's bytes are already in PRG format (2-byte load address
    followed by payload), so we forward them verbatim. ``filename``
    defaults to the basename of ``swm_path`` uppercased and INCLUDES
    the ``.SWM`` extension — SID-Wizard's ``regname`` routine appends
    ``.SWM`` to any user-typed filename before opening the file, so
    the on-disk name must carry the extension too.

    Returns the on-disk filename so callers driving the file dialog
    know what (without extension) to type.
    """
    with open(swm_path, "rb") as fp:
        swm_bytes = fp.read()
    if filename is None:
        import os

        stem = os.path.splitext(os.path.basename(swm_path))[0].upper()
        filename = stem + ".SWM"
    write_d64_with_prg(out_path, swm_bytes, filename)
    return filename


# Convenience re-export so callers can compute layout without
# re-deriving the geometry table.
__all__ = [
    "D64_SIZE",
    "SECTOR_SIZE",
    "TOTAL_SECTORS",
    "SECTORS_PER_TRACK",
    "build_d64_with_prg",
    "write_d64_with_prg",
    "write_d64_with_swm",
]
