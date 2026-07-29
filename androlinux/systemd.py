"""systemd as PID 1, on the Android kernel.

This is the one part of androlinux that needs more than a chroot. systemd checks
``getpid() == 1`` and exits otherwise, so it needs a PID namespace — which Android
disables in GKI on purpose::

    arch/x86/configs/gki_defconfig:41:  # CONFIG_PID_NS is not set

With a kernel built with ``CONFIG_PID_NS=y`` (see ``kernel/build-kernel.sh``),
``unshare --pid --fork`` makes a child PID 1 in a fresh namespace and systemd
starts normally. It is still the Android kernel and still no virtualisation — a
PID namespace is the same primitive containers use, not a hypervisor.

Once systemd is running it owns a namespace that later commands must *join*
rather than recreate, which is why everything here goes through ``nsenter``
against a recorded pid instead of starting its own chroot.
"""

from __future__ import annotations

from pathlib import Path

from . import config, container
from .adb import Adb, AdbError, Result

_SCRIPTS = Path(__file__).parent / "scripts"

PIDFILE = f"{config.DEVICE_ROOT}/systemd.pid"
LOGFILE = f"{config.DEVICE_ROOT}/systemd.log"


def pid(adb: Adb) -> int | None:
    """The Android-side pid of the running systemd, or None.

    Validated rather than trusted: a recorded pid can be recycled by an unrelated
    process after a reboot, so we confirm the process's root is our rootfs.
    """
    res = adb.script(
        config.preamble(ALX_PIDFILE=PIDFILE, ALX_MNT=config.MOUNT)
        + 'p=$(cat "$ALX_PIDFILE" 2>/dev/null || echo)\n'
          '[ -n "$p" ] || exit 0\n'
          '[ -d "/proc/$p" ] || exit 0\n'
          '[ "$(readlink /proc/$p/root 2>/dev/null)" = "$ALX_MNT" ] || exit 0\n'
          'printf %s "$p"\n',
        root=True, timeout=120, label="systemd-pid",
    )
    text = res.out.strip()
    return int(text) if text.isdigit() else None


def is_running(adb: Adb) -> bool:
    return pid(adb) is not None


def start(adb: Adb, *, quiet: bool = False) -> int:
    """Boot systemd as PID 1 inside the rootfs. Returns its Android-side pid."""
    if not container.is_up(adb):
        container.up(adb, quiet=True)

    script = config.preamble(
        ALX_ROOT=config.DEVICE_ROOT,
        ALX_MNT=config.MOUNT,
    ) + (_SCRIPTS / "systemd-start.sh").read_text()

    res = adb.script(script, root=True, timeout=600, label="systemd-start")
    if not quiet:
        print("\n".join(f"  {ln}" for ln in res.lines()))
    if not res.ok:
        raise AdbError(
            f"systemd did not start (exit {res.code}):\n{res.err.strip() or res.out.strip()}"
        )

    got = pid(adb)
    if got is None:
        raise AdbError("systemd reported success but no running process was found")
    return got


def stop(adb: Adb) -> None:
    """Shut systemd down, giving it a chance to stop its own units first."""
    target = pid(adb)
    if target is None:
        print("  systemd is not running")
        return

    res = adb.script(
        config.preamble(ALX_PID=str(target), ALX_PIDFILE=PIDFILE)
        # SIGRTMIN+3 (37) is systemd's "shut down cleanly" signal; it stops units
        # and exits. Only escalate if it ignores that.
        + 'kill -37 "$ALX_PID" 2>/dev/null || kill -TERM "$ALX_PID" 2>/dev/null\n'
          'i=0\n'
          'while [ -d "/proc/$ALX_PID" ] && [ $i -lt 20 ]; do sleep 1; i=$((i+1)); done\n'
          'if [ -d "/proc/$ALX_PID" ]; then\n'
          '  kill -9 "$ALX_PID" 2>/dev/null\n'
          '  echo "systemd ignored the shutdown signal and was killed"\n'
          'else\n'
          '  echo "systemd shut down cleanly after ${i}s"\n'
          'fi\n'
          'rm -f "$ALX_PIDFILE"\n',
        root=True, timeout=180, label="systemd-stop",
    )
    print("\n".join(f"  {ln}" for ln in res.lines()))


def run_in(adb: Adb, command: str, *, timeout: float | None = 600) -> Result:
    """Run a command inside the running systemd's namespaces.

    ``nsenter`` joins the existing PID and mount namespaces; the chroot then
    lands us in the distro with the namespace's own ``/proc``, which is what makes
    ``systemctl`` able to talk to PID 1.
    """
    target = pid(adb)
    if target is None:
        raise AdbError("systemd is not running — start it with 'androlinux systemd start'")

    staged = f"{config.MOUNT}/tmp/androlinux-systemd-cmd.sh"
    adb.script(
        config.preamble(ALX_DST=staged)
        + 'cat > "$ALX_DST" <<\'ALX_EOF\'\n'
        + "#!/bin/bash\nset -o pipefail\n" + command + "\n"
        + "ALX_EOF\nchmod 755 \"$ALX_DST\"\n",
        root=True, timeout=120, label="stage-systemd-cmd",
    ).check("staging command for the systemd namespace")

    return adb.script(
        config.preamble(ALX_PID=str(target), ALX_MNT=config.MOUNT)
        + 'exec nsenter --target "$ALX_PID" --pid --mount -- \\\n'
          '  chroot "$ALX_MNT" /usr/bin/env -i \\\n'
          '    HOME=/root USER=root TERM="${TERM:-xterm-256color}" LANG=C.UTF-8 \\\n'
          '    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \\\n'
          '    ANDROLINUX=1 \\\n'
          '    /bin/bash /tmp/androlinux-systemd-cmd.sh\n',
        root=True, timeout=timeout, label="systemd-run",
    )


def status(adb: Adb) -> str:
    """Report systemd's own view of itself."""
    target = pid(adb)
    if target is None:
        return ("\n  systemd        not running\n"
                "  next           androlinux systemd start\n")

    res = run_in(
        adb,
        'echo "pid1=$(readlink /proc/1/exe 2>/dev/null || echo unknown)"\n'
        'systemctl is-system-running 2>&1 | sed "s/^/state=/"\n'
        'echo "version=$(systemctl --version 2>/dev/null | head -1)"\n'
        'echo "units=$(systemctl list-units --no-legend --no-pager 2>/dev/null | wc -l) loaded"\n'
        'echo "failed=$(systemctl list-units --state=failed --no-legend --no-pager 2>/dev/null | wc -l)"\n',
        timeout=300,
    )

    out = ["", f"  systemd        running (pid {target} on the Android kernel)"]
    for line in res.lines():
        key, _, val = line.partition("=")
        out.append(f"  {key:<14} {val}")
    out.append("")
    return "\n".join(out)


def enter_argv(adb: Adb, command: list[str] | None = None) -> list[str]:
    """adb argv for an interactive shell inside systemd's namespaces."""
    target = pid(adb)
    if target is None:
        raise AdbError("systemd is not running — start it with 'androlinux systemd start'")

    inner = " ".join(command or ["/bin/bash", "--login"])
    argv = [adb.exe]
    if adb.serial:
        argv += ["-s", adb.serial]
    argv += ["shell", "-t", "-t"]
    remote = (
        f"nsenter --target {target} --pid --mount -- "
        f"chroot {config.MOUNT} /usr/bin/env -i "
        f"HOME=/root USER=root TERM=xterm-256color LANG=C.UTF-8 "
        f"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
        f"ANDROLINUX=1 {inner}"
    )
    if adb.root_method == "su":
        argv += [f"su -c '{remote}'"]
    else:
        argv += [remote]
    return argv
