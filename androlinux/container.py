"""Running the Linux userspace on the Android kernel.

There is no virtual machine and no syscall translation here. ``up`` mounts the
rootfs and attaches the kernel's own ``/proc`` and ``/sys`` to it; processes
started inside are ordinary Android-kernel processes that happen to see a Debian
filesystem as ``/``. That is the whole trick, and it is why everything runs at
native speed.

What this cannot do is run systemd. systemd refuses to start unless it is PID 1,
and being PID 1 requires a PID namespace, which Android kernels commonly compile
out (``CONFIG_PID_NS`` unset). :func:`androlinux.probe.Capabilities.decide`
detects that and picks the ``chroot`` strategy, which is what this module
implements.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from . import config
from .adb import Adb, AdbError, Result

_SCRIPTS = Path(__file__).parent / "scripts"

#: Exposed by default: sound, FUSE (for sshfs and friends), and tun (for VPNs).
#: GPU nodes are included so they are used when the kernel has them.
DEFAULT_HW = ("snd", "fuse", "tun", "dri")


#: Re-exported so callers that build device scripts have one obvious source.
_preamble = config.preamble


def up_script(*, hw: tuple[str, ...] = DEFAULT_HW, setenforce: bool = False) -> str:
    """Render up.sh with its inputs bound.

    Both `up` and `autostart` need this exact text, and they must not drift: the
    boot hook stages its own standalone copy, so a variable added here and missed
    there fails only at the next reboot, where nobody is watching. That happened
    once already — ALX_DNS was added for `up` and not for the hook, and `set -u`
    killed the script after it had already mounted the rootfs.
    """
    return _preamble(
        ALX_ROOT=config.DEVICE_ROOT,
        ALX_IMG=config.IMAGE,
        ALX_MNT=config.MOUNT,
        ALX_HW=" ".join(hw),
        ALX_SETENFORCE="1" if setenforce else "0",
        ALX_DNS=" ".join(config.DEFAULT_DNS),
    ) + (_SCRIPTS / "up.sh").read_text()


def up(adb: Adb, *, hw: tuple[str, ...] = DEFAULT_HW, quiet: bool = False,
       setenforce: bool = False) -> Result:
    """Mount the rootfs and prepare it for use. Idempotent."""
    adb.acquire_root()
    script = up_script(hw=hw, setenforce=setenforce)

    res = adb.script(script, root=True, timeout=300, label="up")
    if not quiet:
        sys.stdout.write(res.out)
    if not res.ok:
        raise AdbError(f"bringing the rootfs up failed (exit {res.code}):\n"
                       f"{res.err.strip() or res.out.strip()}")
    return res


def down(adb: Adb, *, quiet: bool = False) -> Result:
    """Unmount everything and release the loop device."""
    adb.acquire_root()
    script = _preamble(ALX_MNT=config.MOUNT, ALX_ROOT=config.DEVICE_ROOT) \
        + (_SCRIPTS / "down.sh").read_text()
    res = adb.script(script, root=True, timeout=300, label="down")
    if not quiet:
        sys.stdout.write(res.out)
    return res


def resize(adb: Adb, size: str) -> Result:
    """Grow the rootfs image and the filesystem inside it.

    Growing only — shrinking ext4 below its used size loses data. The image is
    sparse, so a larger ceiling costs nothing until it is filled.
    """
    adb.acquire_root()
    if is_up(adb):
        print("  unmounting first")
        down(adb, quiet=True)

    script = _preamble(
        ALX_IMG=config.IMAGE,
        ALX_MNT=config.MOUNT,
        ALX_SIZE=size,
    ) + (_SCRIPTS / "resize.sh").read_text()

    res = adb.script(script, root=True, timeout=1800, label="resize")
    sys.stdout.write(res.out)
    if not res.ok:
        raise AdbError(f"resize failed (exit {res.code}):\n{res.err.strip() or res.out.strip()}")
    return res


def remove(adb: Adb, *, everything: bool = False) -> Result:
    """Delete an instance's rootfs, or every instance under the base directory.

    Irreversible. Ordering is what makes it clean rather than merely quick — see
    scripts/remove.sh.
    """
    adb.acquire_root()
    target = config.BASE if everything else config.DEVICE_ROOT
    script = _preamble(
        ALX_BASE=config.BASE,
        ALX_TARGET=target,
        ALX_ALL="1" if everything else "0",
    ) + (_SCRIPTS / "remove.sh").read_text()

    res = adb.script(script, root=True, timeout=600, label="remove")
    sys.stdout.write(res.out)
    if not res.ok:
        raise AdbError(f"removal failed (exit {res.code}):\n{res.err.strip() or res.out.strip()}")
    return res


def is_up(adb: Adb) -> bool:
    res = adb.sh(f"grep -c ' {config.MOUNT} ' /proc/mounts", root=True, timeout=30)
    return res.out.strip() not in ("", "0")


def run(adb: Adb, command: str, *, timeout: float | None = 900,
        user: str | None = None) -> Result:
    """Run a command inside the rootfs, non-interactively.

    The command is delivered as a file to the rootfs's own shell, so it may
    contain quotes, newlines and pipelines without adb mangling it.
    """
    if not is_up(adb):
        up(adb, quiet=True)

    body = "#!/bin/bash\nset -o pipefail\n" + command + "\n"

    # The staged filename must be unique per call. Staging and executing are two
    # separate adb round trips, so a fixed name lets a concurrent `run` — another
    # session, or a parallel agent — overwrite the payload in between, and this
    # process then executes someone else's command and reports it as its own.
    name = f"androlinux-cmd-{uuid.uuid4().hex[:8]}.sh"
    staged = f"{config.MOUNT}/tmp/{name}"

    # `set -e` in the staging script matters: without it the script's status is
    # that of the final `chmod`, so a failed `cat` (a full image, say) would still
    # look successful and leave whatever the file previously held to be executed.
    write = adb.script(
        _preamble(ALX_DST=staged)
        + "set -e\n"
        + 'cat > "$ALX_DST" <<\'ALX_EOF\'\n' + body + "ALX_EOF\n"
        + 'chmod 755 "$ALX_DST"\n',
        root=True, timeout=120, label="stage-cmd",
    )
    write.check("staging command inside rootfs")

    try:
        # `su -` rather than setpriv: it builds a login environment, so the
        # user's own .profile runs and per-user tooling like pyenv and nvm is on
        # PATH. It also applies the account's supplementary groups, which is what
        # gives a non-root user network access at all under Android.
        inner = ('exec "$ALX_HELPER" /bin/bash "$ALX_INNER"\n' if user is None
                 else 'exec "$ALX_HELPER" /bin/su - "$ALX_USER" -c "bash $ALX_INNER"\n')
        return adb.script(
            _preamble(ALX_HELPER=f"{config.DEVICE_ROOT}/chroot-exec",
                      ALX_INNER=f"/tmp/{name}",
                      ALX_USER=user or "")
            + inner,
            root=True, timeout=timeout, label="run",
        )
    finally:
        if not os.environ.get("ANDROLINUX_KEEP_SCRIPTS"):
            adb.sh(f"rm -f {staged}", root=True, timeout=60)


def enter(adb: Adb, command: list[str] | None = None, *, user: str | None = None) -> int:
    """Hand the terminal to a shell inside the rootfs.

    This replaces the current process with adb so the user gets a real pty —
    job control, colours, curses and readline all behave. Nothing is captured,
    which is exactly what an interactive session needs.
    """
    if not is_up(adb):
        up(adb, quiet=True)

    helper = f"{config.DEVICE_ROOT}/chroot-exec"
    if user:
        # A login shell for the user, or their shell running the given command.
        target = ["/bin/su", "-", user] + (["-c", " ".join(command)] if command else [])
    else:
        target = command or ["/bin/bash", "--login"]

    argv = [adb.exe]
    if adb.serial:
        argv += ["-s", adb.serial]
    # -t -t forces a pty even though adb's stdin here is not a terminal.
    argv += ["shell", "-t", "-t"]
    if adb.root_method == "su":
        argv += ["su", "-c", " ".join(["sh", helper, *target])]
    else:
        argv += ["sh", helper, *target]

    sys.stdout.flush()
    os.execvp(argv[0], argv)
    return 0  # unreachable


def status(adb: Adb) -> str:
    """Describe the current state of the rootfs on the target."""
    adb.acquire_root()
    out: list[str] = [""]

    img = adb.sh(f"stat -c '%s %y' {config.IMAGE} 2>/dev/null", root=True, timeout=60).out.strip()
    if not img:
        out.append(f"  rootfs image   not installed ({config.IMAGE} absent)")
        out.append("  next           androlinux install")
        out.append("")
        return "\n".join(out)

    apparent = int(img.split()[0])
    actual = adb.sh(f"du -k {config.IMAGE} 2>/dev/null", root=True, timeout=120).out.split()
    on_disk = int(actual[0]) / 1024 / 1024 if actual else 0.0
    out.append(f"  rootfs image   {config.IMAGE}")
    out.append(f"                 {apparent / 1e9:.1f} GB provisioned, "
               f"{on_disk:.2f} GB actually used on /data (sparse)")

    if not is_up(adb):
        out.append("  state          down")
        out.append("  next           androlinux up")
        out.append("")
        return "\n".join(out)

    out.append("  state          up")
    mounts = adb.sh(f"grep ' {config.MOUNT}' /proc/mounts", root=True, timeout=60).lines()
    out.append(f"  mounts         {len(mounts)}")
    for m in mounts:
        f = m.split()
        out.append(f"                 {f[1].replace(config.MOUNT, '') or '/':<16} {f[2]}")

    ident = run(adb, "cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d= -f2- ; "
                     "uname -srm ; df -h / | tail -1 | tr -s ' '", timeout=120)
    if ident.ok:
        for line in ident.lines():
            out.append(f"  guest          {line.strip(chr(34))}")
    out.append("")
    return "\n".join(out)
