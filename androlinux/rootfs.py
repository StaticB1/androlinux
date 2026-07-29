"""Fetching a distro rootfs and installing it onto the target.

Images come from the linuxcontainers image server, which publishes plain
``rootfs.tar.xz`` trees per distro/release/architecture. They are ordinary
filesystem tarballs — no container runtime is involved in using one.

One wrinkle drives the shape of this module: **Android has no ``xz``.** The probe
confirms ``gzip`` and ``tar`` are present but ``xz`` is not, so the tarball is
recompressed to gzip on the host before being pushed. Recompressing costs about
60 MB of extra transfer on a Debian base, which is cheaper than pushing an
uncompressed tar and far cheaper than shipping an xz binary for every
architecture.
"""

from __future__ import annotations

import gzip
import lzma
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config
from .adb import Adb, AdbError

_IMAGE_SERVER = "https://images.linuxcontainers.org/images"
_BUILD_RE = re.compile(r"\b(\d{8}_\d{2}:\d{2})\b")


@dataclass(frozen=True)
class Source:
    distro: str
    release: str
    arch: str

    @property
    def slug(self) -> str:
        return f"{self.distro}-{self.release}-{self.arch}"

    @property
    def index_url(self) -> str:
        return f"{_IMAGE_SERVER}/{self.distro}/{self.release}/{self.arch}/default/"


#: Releases we have actually exercised. Others usually work — the image server
#: layout is uniform — so this is a convenience list, not a restriction.
KNOWN = {
    "debian": ("bookworm", "trixie", "sid"),
    "ubuntu": ("noble", "jammy"),
    "alpine": ("3.20", "edge"),
}


def resolve_build(src: Source, *, timeout: float = 60) -> str:
    """Return the newest build timestamp published for this source."""
    try:
        with urllib.request.urlopen(src.index_url, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"no image tree for {src.slug} ({exc.code} at {src.index_url}).\n"
            f"  known {src.distro} releases: {', '.join(KNOWN.get(src.distro, ('?',)))}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"cannot reach the image server: {exc}") from exc

    builds = sorted(set(_BUILD_RE.findall(html)))
    if not builds:
        raise RuntimeError(f"no builds listed at {src.index_url}")
    return builds[-1]


def download(src: Source, *, force: bool = False, quiet: bool = False) -> Path:
    """Fetch ``rootfs.tar.xz`` into the host cache and return its path."""
    config.CACHE.mkdir(parents=True, exist_ok=True)
    dest = config.CACHE / f"{src.slug}.tar.xz"
    if dest.exists() and not force:
        if not quiet:
            print(f"  cached  {dest.name} ({dest.stat().st_size / 1e6:.0f} MB)")
        return dest

    build = resolve_build(src)
    url = f"{src.index_url}{build}/rootfs.tar.xz"
    if not quiet:
        print(f"  fetching {src.slug} build {build}")

    tmp = dest.with_suffix(".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with open(tmp, "wb") as fh:
                while chunk := resp.read(1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if not quiet and total:
                        pct = done * 100 // total
                        print(f"\r  downloading {pct:3d}%  "
                              f"{done / 1e6:.0f}/{total / 1e6:.0f} MB", end="", flush=True)
        if not quiet:
            print()
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"download failed: {exc}") from exc

    tmp.replace(dest)
    return dest


def to_gzip(xz_path: Path, *, quiet: bool = False) -> Path:
    """Recompress an xz tarball to gzip, because the target cannot read xz.

    Uses the host's ``xz``/``gzip`` when present since they are markedly faster
    than the stdlib, and falls back to pure Python so the tool still works on a
    host without them.
    """
    gz_path = xz_path.with_suffix("").with_suffix(".tar.gz")
    if gz_path.exists() and gz_path.stat().st_mtime >= xz_path.stat().st_mtime:
        if not quiet:
            print(f"  cached  {gz_path.name} ({gz_path.stat().st_size / 1e6:.0f} MB)")
        return gz_path

    if not quiet:
        print("  recompressing xz to gzip (the target has no xz) ...")

    tmp = gz_path.with_suffix(".part")
    if shutil.which("xz") and shutil.which("gzip"):
        with open(tmp, "wb") as out:
            xz = subprocess.Popen(["xz", "-dc", str(xz_path)], stdout=subprocess.PIPE)
            gz = subprocess.Popen(["gzip", "-1"], stdin=xz.stdout, stdout=out)
            xz.stdout.close()
            gz.communicate()
            xz.wait()
        if gz.returncode != 0 or xz.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("recompression failed (xz/gzip returned non-zero)")
    else:
        with lzma.open(xz_path, "rb") as src, gzip.open(tmp, "wb", compresslevel=1) as dst:
            shutil.copyfileobj(src, dst, length=1 << 20)

    tmp.replace(gz_path)
    if not quiet:
        print(f"  ready   {gz_path.name} ({gz_path.stat().st_size / 1e6:.0f} MB)")
    return gz_path


def install(
    adb: Adb,
    src: Source,
    *,
    size: str = config.DEFAULT_SIZE,
    hostname: str = "androlinux",
    dns: tuple[str, ...] = config.DEFAULT_DNS,
    force_download: bool = False,
    setenforce: bool = False,
) -> None:
    """Fetch, transfer and unpack a rootfs on the target."""
    xz = download(src, force=force_download)
    gz = to_gzip(xz)

    adb.acquire_root()
    adb.sh(f"mkdir -p {config.STAGE}", root=True, timeout=60).check("staging dir")

    remote_tar = f"{config.STAGE}/{gz.name}"
    existing = adb.sh(f"stat -c %s {remote_tar} 2>/dev/null", root=True, timeout=60)
    if existing.out.strip() == str(gz.stat().st_size):
        print(f"  tarball already on target ({gz.stat().st_size / 1e6:.0f} MB)")
    else:
        print(f"  pushing {gz.name} ({gz.stat().st_size / 1e6:.0f} MB) ...")
        # push_root, not push: adbd runs as the shell user on a user build even
        # with su root, so it cannot write under /data/androlinux directly.
        adb.push_root(str(gz), remote_tar).check("push rootfs tarball")

    script = config.preamble(
        ALX_ROOT=config.DEVICE_ROOT,
        ALX_IMG=config.IMAGE,
        ALX_MNT=config.MOUNT,
        ALX_TARBALL=remote_tar,
        ALX_SIZE=size,
        ALX_HOSTNAME=hostname,
        ALX_DNS=" ".join(dns),
        ALX_SETENFORCE="1" if setenforce else "0",
    ) + (Path(__file__).parent / "scripts" / "install.sh").read_text()

    res = adb.script(script, root=True, timeout=1800, label="install")
    sys.stdout.write(res.out)
    if not res.ok:
        raise AdbError(
            f"rootfs install failed (exit {res.code}):\n{res.err.strip() or res.out.strip()}"
        )

    # Exit status alone is not proof of success here. If the device disconnects
    # mid-script — a cable knock, or a tablet browning out under load — adb
    # returns whatever output it had already received and *exits 0*. The install
    # then looks like it worked while having done almost nothing.
    #
    # So verify the postcondition on the device instead of trusting the status.
    verify = adb.script(
        config.preamble(ALX_IMG=config.IMAGE, ALX_MNT=config.MOUNT)
        + 'test -f "$ALX_IMG" || { echo "MISSING_IMAGE"; exit 1; }\n'
          'printf \'img_bytes=%s\\n\' "$(stat -c %s "$ALX_IMG")"\n'
          '{ [ -x "$ALX_MNT/bin/sh" ] || [ -L "$ALX_MNT/bin/sh" ]; } \\\n'
          '  || { echo "MISSING_ROOTFS"; exit 1; }\n'
          'echo verified_ok\n',
        root=True, timeout=300, label="install-verify",
    )
    if "verified_ok" not in verify.out:
        detail = (verify.out + verify.err).strip() or "<no output — the device may have gone away>"
        raise AdbError(
            "the install did not complete.\n"
            f"  {detail}\n\n"
            "  If the target disconnected or rebooted part-way, adb can report success\n"
            "  for a script that never finished. Reconnect and run install again — it is\n"
            "  idempotent and will reuse whatever is already in place. To start the\n"
            f"  filesystem from scratch instead, delete {config.IMAGE} first."
        )


def parse_source(spec: str, arch: str) -> Source:
    """Parse a ``distro[:release]`` spec, e.g. ``debian:bookworm``."""
    distro, _, release = spec.partition(":")
    distro = distro.lower()
    if not release:
        release = KNOWN.get(distro, ("",))[0]
        if not release:
            raise RuntimeError(
                f"unknown distro {distro!r}; give an explicit release like "
                f"'{distro}:<release>'"
            )
    return Source(distro, release, arch)
