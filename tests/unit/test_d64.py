"""Offline tests for the d64 image builder.

Asserts structural invariants we care about. Tests that involve booting
a Commodore DOS implementation against the produced image (round-trip
through c1541) live in tests/integration/, not here.
"""

from __future__ import annotations

import pytest

from sidwizard_driver.d64 import (
    D64_SIZE,
    PRG_START_TRACK,
    SECTOR_SIZE,
    SECTORS_PER_TRACK,
    _track_sector_offset,
    build_d64_with_prg,
)


def test_image_size_is_174848():
    img = build_d64_with_prg(b"\x00\x80hello", "TEST")
    assert len(img) == 174848
    assert len(img) == D64_SIZE


def test_sectors_per_track_totals_683():
    assert sum(SECTORS_PER_TRACK) == 683


def test_track_sector_offset_is_monotonic():
    """Adjacent sectors should be exactly SECTOR_SIZE bytes apart."""
    prev = _track_sector_offset(1, 0)
    assert prev == 0
    for sector in range(1, 21):
        cur = _track_sector_offset(1, sector)
        assert cur - prev == SECTOR_SIZE
        prev = cur


def test_track_sector_offset_rejects_invalid():
    with pytest.raises(ValueError, match="track out of range"):
        _track_sector_offset(0, 0)
    with pytest.raises(ValueError, match="track out of range"):
        _track_sector_offset(36, 0)
    with pytest.raises(ValueError, match="sector"):
        _track_sector_offset(1, 21)  # track 1 has 21 sectors (0..20)


def test_bam_marks_directory_track_full_and_prg_track_partial():
    payload = b"\x00\x80" + b"X" * 500  # small but multi-sector
    img = build_d64_with_prg(payload, "T")
    bam = img[_track_sector_offset(18, 0) : _track_sector_offset(18, 0) + SECTOR_SIZE]

    # Track 18 (directory) entry: free count must be 0.
    t18_slot = 4 + (18 - 1) * 4
    assert bam[t18_slot] == 0

    # Track PRG_START_TRACK: free count should equal sectors_per_track
    # minus the sectors we used (one sector per 254-byte chunk).
    sectors_used = (len(payload) + 253) // 254
    spt = SECTORS_PER_TRACK[PRG_START_TRACK - 1]
    slot = 4 + (PRG_START_TRACK - 1) * 4
    assert bam[slot] == spt - sectors_used


def test_bam_link_points_to_first_dir_sector():
    img = build_d64_with_prg(b"\x00\x80hi", "T")
    bam = img[_track_sector_offset(18, 0) : _track_sector_offset(18, 0) + SECTOR_SIZE]
    assert bam[0] == 18  # next track = 18
    assert bam[1] == 1  # next sector = 1 (first dir sector)
    assert bam[2] == 0x41  # DOS version 'A'


def test_directory_entry_has_correct_file_type_and_pointer():
    img = build_d64_with_prg(b"\x00\x80hi", "MYTUNE")
    dir_sec = img[_track_sector_offset(18, 1) : _track_sector_offset(18, 1) + SECTOR_SIZE]
    # First entry begins at offset 2.
    assert dir_sec[0] == 0  # last directory sector
    assert dir_sec[1] == 0xFF
    assert dir_sec[2] == 0x82  # closed PRG
    assert dir_sec[3] == PRG_START_TRACK
    assert dir_sec[4] == 0  # PRG_START_SECTOR
    # Filename is padded with $A0 (shifted space).
    name = dir_sec[5:21]
    assert name.startswith(b"MYTUNE")
    assert all(b == 0xA0 for b in name[6:])


def test_directory_filename_uppercased():
    img = build_d64_with_prg(b"\x00\x80hi", "flashitback")
    dir_sec = img[_track_sector_offset(18, 1) : _track_sector_offset(18, 1) + SECTOR_SIZE]
    assert dir_sec[5:16] == b"FLASHITBACK"


def test_single_sector_payload_has_correct_terminator():
    """Last (only) sector: next_track=0, next_sector=len(data)+1."""
    data = b"\x00\x80" + b"\xab" * 10  # 12 bytes total, fits one sector
    img = build_d64_with_prg(data, "T")
    off = _track_sector_offset(PRG_START_TRACK, 0)
    sector = img[off : off + SECTOR_SIZE]
    assert sector[0] == 0  # no next track
    assert sector[1] == len(data) + 1  # last-byte-used + 1
    assert sector[2 : 2 + len(data)] == data


def test_multi_sector_payload_chains_correctly():
    """A 600-byte payload spans 3 sectors (254 + 254 + 92). First two
    sectors link to the next on the same track; last sector terminates."""
    data = bytes(range(256)) * 3  # 768 bytes
    data = data[:600]
    img = build_d64_with_prg(data, "T")
    # Sector 0 of track 17: first chunk.
    off0 = _track_sector_offset(PRG_START_TRACK, 0)
    s0 = img[off0 : off0 + SECTOR_SIZE]
    assert s0[0] == PRG_START_TRACK  # link to same track
    assert s0[1] == 1  # next sector
    assert s0[2 : 2 + 254] == data[:254]
    # Sector 1
    off1 = _track_sector_offset(PRG_START_TRACK, 1)
    s1 = img[off1 : off1 + SECTOR_SIZE]
    assert s1[0] == PRG_START_TRACK
    assert s1[1] == 2
    assert s1[2 : 2 + 254] == data[254 : 254 + 254]
    # Sector 2: last
    off2 = _track_sector_offset(PRG_START_TRACK, 2)
    s2 = img[off2 : off2 + SECTOR_SIZE]
    assert s2[0] == 0
    assert s2[1] == (600 - 254 * 2) + 1  # bytes in last sector + 1
    assert s2[2 : 2 + (600 - 254 * 2)] == data[254 * 2 : 600]


def test_disk_name_petscii_padded():
    img = build_d64_with_prg(b"\x00\x80x", "T", disk_name="HELLO")
    bam = img[_track_sector_offset(18, 0) : _track_sector_offset(18, 0) + SECTOR_SIZE]
    assert bam[0x90:0x95] == b"HELLO"
    assert all(b == 0xA0 for b in bam[0x95:0xA0])
    assert bam[0xA2:0xA4] == b"01"


def test_rejects_empty_payload():
    with pytest.raises(ValueError, match="empty"):
        build_d64_with_prg(b"", "T")


def test_rejects_oversize_payload():
    # 22 sectors * 254 bytes > 21 sectors available on track 17.
    too_big = b"\x00\x80" + b"X" * (22 * 254)
    with pytest.raises(ValueError, match="too large"):
        build_d64_with_prg(too_big, "T")


def test_rejects_non_ascii_filename():
    with pytest.raises(ValueError, match="non-printable"):
        build_d64_with_prg(b"\x00\x80x", "café")  # 'é' is outside printable ASCII


def test_rejects_filename_too_long():
    with pytest.raises(ValueError, match="too long"):
        build_d64_with_prg(b"\x00\x80x", "A" * 17)


def test_real_swm_files_fit_in_one_track():
    """The four sample .swm files shipped in pysidwizard must all fit
    in a single 21-sector chain (= 5334 bytes). Sanity check so we
    don't silently lose tunes when a future SWM is too big."""
    import os

    sample_dir = "/scratch/anarkiwi/pysidwizard/tests/data"
    if not os.path.isdir(sample_dir):
        pytest.skip("pysidwizard sample data not available on this host")
    for name in os.listdir(sample_dir):
        if not name.endswith(".swm"):
            continue
        path = os.path.join(sample_dir, name)
        with open(path, "rb") as fp:
            blob = fp.read()
        sectors = (len(blob) + 253) // 254
        assert sectors <= 21, f"{name} needs {sectors} sectors (>21)"
        # Round-trip: building succeeds.
        img = build_d64_with_prg(blob, name.upper().replace(".SWM", "")[:16])
        assert len(img) == D64_SIZE
