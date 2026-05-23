"""Offline tests for capture-side helpers."""

from __future__ import annotations

import pytest

from sidwizard_driver.capture import SWM_FRAMESPEED_OFFSET, _read_swm_framespeed


def _make_swm(framespeed: int) -> bytes:
    """Build the minimum bytes needed to extract framespeed: 2-byte PRG
    load + 4-byte magic + 1-byte framespeed."""
    header = bytearray(SWM_FRAMESPEED_OFFSET + 1)
    header[0:2] = b"\xf8\x1f"  # $1FF8 PRG load address
    header[2:6] = b"SWM1"
    header[SWM_FRAMESPEED_OFFSET] = framespeed
    return bytes(header)


def test_read_swm_framespeed_returns_byte(tmp_path):
    for fs in (1, 2, 3, 4):
        path = tmp_path / f"fs{fs}.swm"
        path.write_bytes(_make_swm(fs))
        assert _read_swm_framespeed(str(path)) == fs


def test_read_swm_framespeed_rejects_out_of_range(tmp_path):
    for bad in (0, 5, 99):
        path = tmp_path / f"bad{bad}.swm"
        path.write_bytes(_make_swm(bad))
        with pytest.raises(ValueError, match="unreasonable framespeed"):
            _read_swm_framespeed(str(path))


def test_read_swm_framespeed_rejects_truncated(tmp_path):
    path = tmp_path / "short.swm"
    path.write_bytes(b"\xf8\x1fSW")  # only 4 bytes
    with pytest.raises(ValueError, match="too short"):
        _read_swm_framespeed(str(path))
