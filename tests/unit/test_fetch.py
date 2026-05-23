"""Offline tests for the SID-Wizard d64 fetcher.

The download itself is exercised by the live smoke; here we only cover
cache lookup, SHA-256 verification, and default-path resolution.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sidwizard_driver import fetch
from sidwizard_driver.fetch import (
    DISK1_FILENAME,
    DISK1_SHA256,
    _sha256,
    default_cache_dir,
    fetch_disk1_d64,
)


def _write_with_sha(path: Path, content: bytes) -> str:
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def test_default_cache_dir_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert default_cache_dir() == tmp_path / "xdg" / "sidwizard-driver"


def test_default_cache_dir_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_cache_dir() == tmp_path / ".cache" / "sidwizard-driver"


def test_sha256_matches_hashlib(tmp_path):
    blob = b"the quick brown fox jumps over the lazy dog\n" * 32
    path = tmp_path / "f.bin"
    expect = _write_with_sha(path, blob)
    assert _sha256(path) == expect


def test_fetch_returns_cached_d64_without_network(tmp_path, monkeypatch):
    """If a valid disk1.d64 already exists in cache, fetch is a no-op
    and never touches the network."""
    cache = tmp_path / "cache"
    cache.mkdir()
    # Fake disk1.d64 whose sha matches the pinned constant — patch the
    # constant to match a synthetic blob.
    blob = b"fake d64 payload"
    fake_sha = hashlib.sha256(blob).hexdigest()
    (cache / DISK1_FILENAME).write_bytes(blob)
    monkeypatch.setattr(fetch, "DISK1_SHA256", fake_sha)

    def fail_download(*a, **kw):
        raise AssertionError("network should not be touched")

    monkeypatch.setattr(fetch, "_download", fail_download)
    result = fetch_disk1_d64(cache_dir=cache)
    assert result == cache / DISK1_FILENAME


def test_fetch_redownloads_when_cache_corrupt(tmp_path, monkeypatch):
    """If disk1.d64 is present but the SHA doesn't match, fetch should
    re-download (i.e. invoke _download, which we mock out)."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / DISK1_FILENAME).write_bytes(b"corrupt")
    download_calls: list[str] = []

    def fake_download(url, dest):
        download_calls.append(url)
        # Write a fake tarball that the post-download SHA check will
        # reject — so we exit early with the expected RuntimeError.
        dest.write_bytes(b"not a real tarball")

    monkeypatch.setattr(fetch, "_download", fake_download)
    with pytest.raises(RuntimeError, match="tarball SHA-256 mismatch"):
        fetch_disk1_d64(cache_dir=cache)
    assert download_calls == [fetch.SIDWIZARD_TARBALL_URL]


def test_fetch_rejects_tarball_sha_mismatch(tmp_path, monkeypatch):
    cache = tmp_path / "cache"

    def fake_download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"wrong content")

    monkeypatch.setattr(fetch, "_download", fake_download)
    with pytest.raises(RuntimeError, match="tarball SHA-256 mismatch"):
        fetch_disk1_d64(cache_dir=cache)
    # The corrupt tarball must be removed so the next call retries.
    assert not (cache / "SID-Wizard-1.94-with-sources.tar.gz").exists()


def test_pinned_disk1_sha_is_well_formed():
    """Guard against accidental edits to the pinned hex digest."""
    assert len(DISK1_SHA256) == 64
    int(DISK1_SHA256, 16)


def _make_tarball(dest: Path, member_path: str, member_content: bytes) -> bytes:
    """Build a tiny gzip-tar at ``dest`` carrying one file. Returns its raw bytes."""
    import io
    import tarfile as tf

    buf = io.BytesIO()
    with tf.open(fileobj=buf, mode="w:gz") as t:
        info = tf.TarInfo(name=member_path)
        info.size = len(member_content)
        t.addfile(info, io.BytesIO(member_content))
    raw = buf.getvalue()
    dest.write_bytes(raw)
    return raw


def test_fetch_extracts_and_verifies_disk1(tmp_path, monkeypatch):
    """Full extract path: pre-stage a synthetic tarball whose SHA-256
    matches the pinned constant (after we monkey-patch the constant)
    so fetch_disk1_d64 skips the download, extracts the inner file,
    and verifies its inner SHA-256."""
    cache = tmp_path / "cache"
    cache.mkdir()

    inner = b"i am pretending to be a c64 disk image"
    inner_sha = hashlib.sha256(inner).hexdigest()

    tarball = cache / "SID-Wizard-1.94-with-sources.tar.gz"
    raw = _make_tarball(tarball, fetch.DISK1_TAR_MEMBER, inner)
    tarball_sha = hashlib.sha256(raw).hexdigest()

    monkeypatch.setattr(fetch, "SIDWIZARD_TARBALL_SHA256", tarball_sha)
    monkeypatch.setattr(fetch, "DISK1_SHA256", inner_sha)

    result = fetch_disk1_d64(cache_dir=cache)
    assert result == cache / DISK1_FILENAME
    assert result.read_bytes() == inner


def test_fetch_rejects_inner_sha_mismatch(tmp_path, monkeypatch):
    """If the tarball is intact but the extracted d64's SHA-256 is
    wrong, the extracted file must be deleted and the call must
    raise."""
    cache = tmp_path / "cache"
    cache.mkdir()

    inner = b"wrong inner content"
    tarball = cache / "SID-Wizard-1.94-with-sources.tar.gz"
    raw = _make_tarball(tarball, fetch.DISK1_TAR_MEMBER, inner)
    tarball_sha = hashlib.sha256(raw).hexdigest()

    monkeypatch.setattr(fetch, "SIDWIZARD_TARBALL_SHA256", tarball_sha)
    # Leave DISK1_SHA256 at its real value — won't match the synthetic inner.
    with pytest.raises(RuntimeError, match="disk1.d64 SHA-256 mismatch"):
        fetch_disk1_d64(cache_dir=cache)
    assert not (cache / DISK1_FILENAME).exists()


def test_main_prints_path(tmp_path, monkeypatch, capsys):
    fake = tmp_path / "fake.d64"
    fake.write_bytes(b"x")
    monkeypatch.setattr(fetch, "fetch_disk1_d64", lambda cache_dir=None: fake)
    rc = fetch.main(["--cache-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == str(fake)
