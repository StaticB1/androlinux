"""Creating and using a normal (non-root) account inside the rootfs.

Root is where a session necessarily begins — ``chroot``, ``mount`` and ``mknod``
are privileged — but it is a poor place to stay. Several desktop applications
refuse to run as root outright (Chromium and Firefox among them), and per-user
tooling like pyenv and nvm expects a real home directory.

The catch is that a distro user created the obvious way has **no network at all**.
Android gates socket creation on group membership rather than capabilities, so a
process must be in ``AID_INET`` (gid 3003) to open one, and a chroot inherits no
Android groups. ``scripts/user-add.sh`` documents the measurement; this module
just drives it.
"""

from __future__ import annotations

from pathlib import Path

from . import config, container
from .adb import Adb, AdbError

_SCRIPTS = Path(__file__).parent / "scripts"

DEFAULT_UID = 1000
DEFAULT_SHELL = "/bin/bash"


def add(
    adb: Adb,
    name: str,
    *,
    uid: int = DEFAULT_UID,
    shell: str = DEFAULT_SHELL,
    sudo: bool = True,
) -> None:
    """Create (or repair) a user inside the rootfs, with working network access."""
    if not name.isascii() or not name.replace("_", "").replace("-", "").isalnum():
        raise AdbError(f"implausible username {name!r}")

    body = config.preamble(
        ALX_USER=name,
        ALX_UID=str(uid),
        ALX_SHELL=shell,
        ALX_SUDO="1" if sudo else "0",
    ) + (_SCRIPTS / "user-add.sh").read_text()

    res = container.run(adb, body, timeout=600)
    print("\n".join(f"  {ln}" for ln in res.lines()))
    if not res.ok:
        raise AdbError(f"could not create {name} (exit {res.code}):\n{res.err.strip()}")

    # Remember the choice on the device, so `enter` can default to it and the
    # boot hook can start the desktop as a human rather than as root.
    adb.script(
        config.preamble(ALX_DST=f"{config.DEVICE_ROOT}/default-user", ALX_USER=name)
        + 'set -e\nprintf "%s\\n" "$ALX_USER" > "$ALX_DST"\n',
        root=True, timeout=120, label="stage-default-user",
    ).check("recording the default user")
    print(f"  recorded as the default user for this instance")


def default(adb: Adb) -> str | None:
    """The user `enter` should use for this instance, if one was set."""
    res = adb.sh(f"cat {config.DEVICE_ROOT}/default-user 2>/dev/null",
                 root=True, timeout=60)
    name = res.out.strip()
    return name or None
