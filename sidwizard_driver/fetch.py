"""Fetch SID-Wizard 1.94's ``disk1.d64`` from CSDB on demand."""

from __future__ import annotations

import hashlib
import logging
import os
import tarfile
import tempfile
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

SIDWIZARD_TARBALL_URL = (
    "https://csdb.dk/getinternalfile.php/276275/SID-Wizard-1.94-with-sources.tar.gz"
)
SIDWIZARD_TARBALL_SHA256 = "544e36aff3fe14b7e4cf81a04c680a6883191a222754b2f0489e15349a89b559"
SIDWIZARD_TARBALL_SIZE = 8984028

DISK1_TAR_MEMBER = "SID-Wizard-1.94/SID-Wizard-1.94-disk1.d64"
DISK1_SHA256 = "4f6896db53c07aec7e6e7377acdb337d93b632b8c965bd37ea88db71216dcc39"
DISK1_FILENAME = "SID-Wizard-1.94-disk1.d64"


def default_cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "sidwizard-driver"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=dest.name + ".",
        suffix=".part",
        dir=dest.parent,
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        log.info("downloading %s", url)
        with urllib.request.urlopen(url) as resp, open(tmp_path, "wb") as out:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
        os.replace(tmp_path, dest)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def fetch_disk1_d64(cache_dir: Path | None = None) -> Path:
    """Return a path to ``SID-Wizard-1.94-disk1.d64``, fetching on demand.

    Idempotent. Raises ``RuntimeError`` if SHA-256 verification fails.
    """
    cache = cache_dir or default_cache_dir()
    disk1 = cache / DISK1_FILENAME
    if disk1.is_file() and _sha256(disk1) == DISK1_SHA256:
        return disk1

    cache.mkdir(parents=True, exist_ok=True)
    tarball = cache / "SID-Wizard-1.94-with-sources.tar.gz"
    if not (tarball.is_file() and _sha256(tarball) == SIDWIZARD_TARBALL_SHA256):
        _download(SIDWIZARD_TARBALL_URL, tarball)
        got = _sha256(tarball)
        if got != SIDWIZARD_TARBALL_SHA256:
            tarball.unlink(missing_ok=True)
            raise RuntimeError(
                f"SID-Wizard tarball SHA-256 mismatch: got {got}, "
                f"expected {SIDWIZARD_TARBALL_SHA256}"
            )

    with tarfile.open(tarball, "r:gz") as tf:
        member = tf.getmember(DISK1_TAR_MEMBER)
        src = tf.extractfile(member)
        if src is None:
            raise RuntimeError(f"tarball member {DISK1_TAR_MEMBER} is not a file")
        with open(disk1, "wb") as out:
            while True:
                chunk = src.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)

    got = _sha256(disk1)
    if got != DISK1_SHA256:
        disk1.unlink(missing_ok=True)
        raise RuntimeError(
            f"extracted disk1.d64 SHA-256 mismatch: got {got}, expected {DISK1_SHA256}"
        )
    return disk1


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cache-dir", type=Path, default=None)
    p.add_argument("-v", "--verbose", action="count", default=0)
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    path = fetch_disk1_d64(cache_dir=args.cache_dir)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
