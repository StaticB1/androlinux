"""Shared paths and layout.

Everything androlinux owns on the target lives under :data:`DEVICE_ROOT`, and the
rootfs itself lives inside a single ext4 image file rather than being unpacked
directly onto ``/data``.

That indirection is not stylistic. ``/data`` is mounted ``nosuid,nodev`` on
Android, which would silently break a distro in two ways: ``sudo`` and every
other setuid binary would lose its privilege bit, and device nodes under the
rootfs's own ``/dev`` would not function. A loopback ext4 image gets its own
mount options, so it can be mounted ``suid,dev,exec`` and behave like a normal
Linux root filesystem.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Base of everything androlinux creates on the target.
BASE = "/data/androlinux"

#: Instances let several distros coexist — an Ubuntu matching your desktop
#: alongside the Debian you started with, say. The unnamed instance keeps the
#: original paths so an existing install is untouched by the feature's arrival.
DEFAULT_INSTANCE = "default"
INSTANCE = DEFAULT_INSTANCE

#: Everything for the *current* instance sits here.
DEVICE_ROOT = BASE

#: The ext4 image holding the distro.
IMAGE = f"{DEVICE_ROOT}/rootfs.img"

#: Where the image is mounted; this is the chroot target.
MOUNT = f"{DEVICE_ROOT}/mnt"

#: Landing area for pushed tarballs.
STAGE = f"{DEVICE_ROOT}/stage"


def use(name: str | None) -> str:
    """Point the module at one instance. Returns the name in effect.

    These stay module-level names rather than becoming a parameter threaded
    through every function because every reader already resolves them at call
    time. Rebinding them once, before the command runs, switches the whole tool
    over without touching a single call site.
    """
    global INSTANCE, DEVICE_ROOT, IMAGE, MOUNT, STAGE

    INSTANCE = name or DEFAULT_INSTANCE
    if INSTANCE == DEFAULT_INSTANCE:
        DEVICE_ROOT = BASE
    else:
        if "/" in INSTANCE or INSTANCE.startswith("."):
            raise ValueError(f"bad instance name {INSTANCE!r}")
        DEVICE_ROOT = f"{BASE}/{INSTANCE}"

    IMAGE = f"{DEVICE_ROOT}/rootfs.img"
    MOUNT = f"{DEVICE_ROOT}/mnt"
    STAGE = f"{DEVICE_ROOT}/stage"
    return INSTANCE

#: Host-side download cache.
CACHE = Path(os.environ.get("ANDROLINUX_CACHE", Path.home() / ".cache" / "androlinux"))

#: Default image size. ext4 on a sparse file, so this is a ceiling, not a
#: reservation — an empty 12G image occupies well under 1G of /data.
DEFAULT_SIZE = "12G"

#: Fallback resolvers written into the rootfs when Android does not advertise
#: its own. Deliberately NOT the emulator's 10.0.2.3 gateway: on a physical
#: device that address is unroutable, and putting it first makes every lookup
#: wait for it to time out. glibc eventually falls through — so `getent` appears
#: to work — while apt gives up first and reports "Temporary failure resolving".
#: The emulator reaches these public resolvers through its NAT anyway.
DEFAULT_DNS = ("1.1.1.1", "8.8.8.8")


def preamble(**variables: str) -> str:
    """Render shell assignments to prepend to a device script.

    The scripts under ``scripts/`` read their inputs from shell variables rather
    than positional arguments, so the host can hand them paths and lists without
    those values passing through adb's argv flattening. Values are single-quoted
    with embedded quotes escaped, so a path containing a space or an apostrophe
    cannot break out of its assignment.
    """
    lines = []
    for key, value in variables.items():
        escaped = str(value).replace("'", "'\\''")
        lines.append(f"{key}='{escaped}'")
    return "\n".join(lines) + "\n"
