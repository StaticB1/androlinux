"""Bringing the rootfs up automatically after the device reboots.

Android gives an unprivileged tool no supported way to run code at boot, so this
depends on how the target was rooted:

**Magisk** (physical devices) executes every script in ``/data/adb/service.d`` as
root during late boot. That is a first-class hook and is what this module
installs.

**``adb root``** (emulators, userdebug builds) has no equivalent. adbd running as
root is a debugging affordance, not a boot mechanism, and nothing on ``/data`` is
executed by init. There is no honest way to fake this, so :func:`enable` says so
and points at the alternatives instead of installing something that will silently
not run.

The installed hook is deliberately self-contained: it renders the current
``up.sh`` onto the device, so a reboot does not need a host, a network, or the
androlinux package to be present anywhere.
"""

from __future__ import annotations

from pathlib import Path

from . import config, container, gui
from .adb import Adb, AdbError

_SCRIPTS = Path(__file__).parent / "scripts"

#: Magisk runs everything here, as root, on every boot.
MAGISK_SERVICE_D = "/data/adb/service.d"
HOOK_NAME = "androlinux.sh"


def _hook_path() -> str:
    return f"{MAGISK_SERVICE_D}/{HOOK_NAME}"


def has_magisk(adb: Adb) -> bool:
    res = adb.sh(f"test -d {MAGISK_SERVICE_D} && echo yes", root=True, timeout=60)
    return res.out.strip() == "yes"


def enable(adb: Adb, *, start_gui: bool = False, hw: tuple[str, ...] = container.DEFAULT_HW,
           geometry: str = gui.DEFAULT_GEOMETRY,
           session: str = gui.DEFAULT_SESSION) -> None:
    """Install the boot hook. Idempotent."""
    adb.acquire_root()

    # 1. A standalone up.sh on the device, with the same values the host would
    #    have injected. Nothing here may depend on the host at boot time.
    up_body = container.up_script(hw=hw)

    adb.script(
        config.preamble(ALX_DST=f"{config.DEVICE_ROOT}/up.sh")
        + f'mkdir -p {config.DEVICE_ROOT}\n'
        + 'cat > "$ALX_DST" <<\'ALX_UP_EOF\'\n' + up_body + "ALX_UP_EOF\n"
        + 'chmod 755 "$ALX_DST"\n',
        root=True, timeout=120, label="stage-up",
    ).check("writing up.sh to the device")

    # 2. If the desktop should come up too, put its start script inside the
    #    rootfs at a path that survives (/tmp does not).
    display = gui.recorded_display(adb) or gui.DEFAULT_DISPLAY

    if start_gui:
        spec = gui.SESSIONS[session]
        gui_body = (
            f"ALX_DISPLAY='{display}'\n"
            f"ALX_GEOMETRY='{geometry}'\n"
            f"ALX_DEPTH='{gui.DEFAULT_DEPTH}'\n"
            f"ALX_SESSION_CMD='{spec['command']}'\n"
            f"ALX_SESSION_ENV='{spec['env']}'\n"
            f"ALX_SESSION_PROC='{spec['proc']}'\n"
            + (_SCRIPTS / "gui-start.sh").read_text()
        )
        if not container.is_up(adb):
            container.up(adb, hw=hw, quiet=True)
        adb.script(
            config.preamble(ALX_DST=f"{config.MOUNT}/usr/local/sbin/androlinux-gui-start")
            + f'mkdir -p {config.MOUNT}/usr/local/sbin\n'
            + 'cat > "$ALX_DST" <<\'ALX_GUI_EOF\'\n' + gui_body + "ALX_GUI_EOF\n"
            + 'chmod 755 "$ALX_DST"\n',
            root=True, timeout=120, label="stage-gui",
        ).check("writing the desktop start script into the rootfs")

    # 3. The boot hook itself.
    boot_body = config.preamble(
        ALX_ROOT=config.DEVICE_ROOT,
        ALX_IMG=config.IMAGE,
        ALX_START_GUI="1" if start_gui else "0",
    ) + (_SCRIPTS / "boot.sh").read_text()

    if not has_magisk(adb):
        adb.script(
            config.preamble(ALX_DST=f"{config.DEVICE_ROOT}/boot.sh")
            + 'cat > "$ALX_DST" <<\'ALX_BOOT_EOF\'\n' + boot_body + "ALX_BOOT_EOF\n"
            + 'chmod 755 "$ALX_DST"\n',
            root=True, timeout=120, label="stage-boot",
        ).check("writing boot.sh to the device")
        raise AdbError(
            "no Magisk on this target, so nothing on /data runs at boot.\n\n"
            f"  Everything needed is staged ({config.DEVICE_ROOT}/boot.sh is ready to run),\n"
            "  but Android's init will not invoke it. This target was rooted with\n"
            "  'adb root', which is a debugging feature and not a boot mechanism.\n\n"
            "  Options:\n"
            "    · On a Magisk-rooted phone, run this again — the hook installs into\n"
            f"      {MAGISK_SERVICE_D} and works properly.\n"
            "    · On an emulator, run 'androlinux up' after boot, or have the host do\n"
            f"      it: adb wait-for-device && adb shell sh {config.DEVICE_ROOT}/boot.sh"
        )

    hook = (
        "#!/system/bin/sh\n"
        "# Installed by androlinux. Runs as root during Magisk's late_start.\n"
        f"exec sh {config.DEVICE_ROOT}/boot.sh\n"
    )
    adb.script(
        config.preamble(ALX_DST=f"{config.DEVICE_ROOT}/boot.sh", ALX_HOOK=_hook_path())
        + 'cat > "$ALX_DST" <<\'ALX_BOOT_EOF\'\n' + boot_body + "ALX_BOOT_EOF\n"
        + 'chmod 755 "$ALX_DST"\n'
        + 'cat > "$ALX_HOOK" <<\'ALX_HOOK_EOF\'\n' + hook + "ALX_HOOK_EOF\n"
        + 'chmod 755 "$ALX_HOOK"\n',
        root=True, timeout=120, label="stage-hook",
    ).check("installing the Magisk boot hook")

    # 4. An on-device entry point, so the device does not need a host to start
    #    everything again after you stop it. Staged here because this is the
    #    command whose job is making a device self-sufficient.
    start_body = config.preamble(
        ALX_ROOT=config.DEVICE_ROOT,
        ALX_VNC_URI=f"vnc://127.0.0.1:{gui.vnc_port(display)}",
    ) + (_SCRIPTS / "start.sh").read_text()

    adb.script(
        config.preamble(ALX_DST=f"{config.DEVICE_ROOT}/start")
        + "set -e\n"
        + "cat > \"$ALX_DST\" <<'ALX_START_EOF'\n" + start_body + "ALX_START_EOF\n"
        + 'chmod 755 "$ALX_DST"\n',
        root=True, timeout=120, label="stage-start",
    ).check("staging the on-device start script")

    print(f"  installed {_hook_path()}")
    print(f"  it runs {config.DEVICE_ROOT}/boot.sh, logging to {config.DEVICE_ROOT}/boot.log")
    if start_gui:
        print(f"  the {session} desktop will start automatically at {geometry} on {display}")
    print(f"\n  to start it from the device itself, with no host attached:")
    print(f"    su -c 'sh {config.DEVICE_ROOT}/start'")
    print("\n  reboot to verify: adb reboot && androlinux status")


def disable(adb: Adb) -> None:
    adb.acquire_root()
    existed = adb.sh(f"test -f {_hook_path()} && echo yes", root=True, timeout=60).out.strip()
    adb.sh(f"rm -f {_hook_path()}", root=True, timeout=60)
    if existed == "yes":
        print(f"  removed {_hook_path()}")
    else:
        print(f"  nothing to remove ({_hook_path()} was not present)")
    print(f"  left {config.DEVICE_ROOT}/boot.sh in place; it is harmless unless invoked")


def status(adb: Adb) -> str:
    adb.acquire_root()
    out = [""]
    magisk = has_magisk(adb)
    out.append(f"  magisk         {'present' if magisk else 'absent'}"
               f"  ({MAGISK_SERVICE_D})")

    hook = adb.sh(f"test -f {_hook_path()} && echo yes", root=True, timeout=60).out.strip()
    out.append(f"  boot hook      {'installed' if hook == 'yes' else 'not installed'}")

    staged = adb.sh(f"test -x {config.DEVICE_ROOT}/up.sh && echo yes",
                    root=True, timeout=60).out.strip()
    out.append(f"  staged up.sh   {'yes' if staged == 'yes' else 'no'}")

    if not magisk:
        out.append("  note           without Magisk nothing on /data runs at boot; "
                   "run 'androlinux up' after boot")

    log = adb.sh(f"tail -6 {config.DEVICE_ROOT}/boot.log 2>/dev/null", root=True, timeout=60)
    if log.out.strip():
        out.append("  last boot log")
        for line in log.lines():
            out.append(f"                 {line}")
    out.append("")
    return "\n".join(out)
