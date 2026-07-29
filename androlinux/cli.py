"""androlinux command line."""

from __future__ import annotations

import argparse
import shlex
import sys

from . import __version__, autostart, config, container, gui, probe, rootfs, sshd, users
from .adb import Adb, AdbError


def _target(args: argparse.Namespace) -> Adb:
    adb = Adb(getattr(args, "serial", None))
    adb.require_device()
    return adb


# ------------------------------------------------------------------- commands


def cmd_probe(args: argparse.Namespace) -> int:
    adb = _target(args)
    caps = probe.run(adb)
    verdict = caps.decide()

    if args.raw:
        for key in sorted(caps.raw):
            print(f"{key}={caps.raw[key]}")
        return 0 if verdict.viable else 1

    print(probe.render(caps, verdict))
    if not caps.complete:
        print("  note: probe output was truncated; the results above are partial\n")
    return 0 if verdict.viable else 1


def cmd_install(args: argparse.Namespace) -> int:
    adb = _target(args)

    arch = args.arch
    if not arch:
        caps = probe.run(adb)
        arch = caps.debian_arch
        if not arch:
            raise AdbError(
                f"could not map the target CPU ({caps.get('kernel.arch')!r}) to a distro "
                "architecture — pass --arch (amd64, arm64, armhf, i386)"
            )
        verdict = caps.decide()
        if not verdict.viable:
            print(probe.render(caps, verdict))
            raise AdbError("this target cannot host a Linux rootfs; see the blockers above")
        print(f"  target is {caps.get('kernel.arch')} → {arch}, strategy {verdict.strategy}")

    src = rootfs.parse_source(args.distro, arch)
    print(f"  installing {src.distro} {src.release} ({src.arch})")
    rootfs.install(
        adb, src,
        size=args.size,
        hostname=args.hostname,
        force_download=args.redownload,
        setenforce=args.permissive,
    )
    print("\n  done — 'androlinux enter' opens a shell inside it\n")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    container.up(_target(args),
                 hw=tuple(args.hw.split(",")) if args.hw else container.DEFAULT_HW,
                 setenforce=args.permissive)
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    container.down(_target(args))
    return 0


def cmd_resize(args: argparse.Namespace) -> int:
    adb = _target(args)
    container.resize(adb, args.size)
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    adb = _target(args)
    scope = "every instance" if args.all else f"instance '{config.INSTANCE}'"
    if not args.yes:
        print(f"  this would permanently delete {scope} and everything inside it.")
        print("  re-run with --yes to go ahead.")
        return 1
    container.remove(adb, everything=args.all)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(container.status(_target(args)))
    return 0


def cmd_enter(args: argparse.Namespace) -> int:
    adb = _target(args)
    adb.acquire_root()
    # Default to the recorded user when one exists: interactive work wants a
    # human account, while --root is there for the times apt is involved.
    user = None if args.root else (args.user or users.default(adb))
    return container.enter(adb, args.argv or None, user=user)


def cmd_run(args: argparse.Namespace) -> int:
    adb = _target(args)
    # A single argument is already a shell command ('ls -la /etc') and must be
    # passed through untouched. Several arguments came from the host shell as
    # separate words, so they need re-quoting or the guest shell re-splits them:
    # run python3 -c "print('a b')" would otherwise reach python as print(a b).
    command = args.argv[0] if len(args.argv) == 1 else shlex.join(args.argv)
    res = container.run(adb, command, user=args.user, timeout=args.timeout)
    sys.stdout.write(res.out)
    sys.stderr.write(res.err)
    return res.code


def cmd_gui(args: argparse.Namespace) -> int:
    adb = _target(args)
    if args.gui_action == "install":
        gui.install(adb, session=args.session)
    elif args.gui_action == "start":
        gui.start(adb, display=args.display, geometry=args.geometry,
                  force=args.restart, user=args.session_user,
                  session=args.session)
    elif args.gui_action == "stop":
        gui.stop(adb, display=args.display)
    elif args.gui_action == "screenshot":
        path = gui.screenshot(adb, args.output, display=args.display,
                              user=args.session_user)
        print(f"  wrote {path}")
    elif args.gui_action == "web":
        gui.web(adb, display=args.display, port=args.port, on_device=args.on_device)
    elif args.gui_action == "viewer":
        gui.viewer(adb, display=args.display, apk=args.apk, reinstall=args.reinstall)
    return 0


# --------------------------------------------------------------------- parser


def cmd_user(args: argparse.Namespace) -> int:
    adb = _target(args)
    if args.user_action == "add":
        users.add(adb, args.username, uid=args.uid, sudo=not args.no_sudo)
    else:
        current = users.default(adb)
        print(f"  default user: {current or '(none — sessions run as root)'}")
    return 0


def cmd_ssh(args: argparse.Namespace) -> int:
    adb = _target(args)
    if args.ssh_action == "enable":
        sshd.enable(adb, port=args.port, user=args.user, key=args.key)
    else:
        print(sshd.status(adb, port=args.port))
    return 0


def cmd_autostart(args: argparse.Namespace) -> int:
    adb = _target(args)
    if args.autostart_action == "enable":
        autostart.enable(adb, start_gui=args.gui, geometry=args.geometry,
                         session=args.session)
    elif args.autostart_action == "disable":
        autostart.disable(adb)
    else:
        print(autostart.status(adb))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="androlinux",
        description="Run a full Linux userspace natively on an Android kernel — no virtualisation.",
    )
    ap.add_argument("--version", action="version", version=f"androlinux {__version__}")
    ap.add_argument("-s", "--serial", help="adb serial, when several targets are attached")
    ap.add_argument("-n", "--name", default=None,
                    help="which rootfs instance to act on; omit for the original one. "
                         "Lets several distros coexist (e.g. --name ubuntu).")

    # --serial is accepted both before and after the subcommand. Typing it at the
    # end is the natural reflex, and without this argparse rejects the whole
    # invocation with a bare "unrecognized arguments: -s ...".
    #
    # default=argparse.SUPPRESS is load-bearing: with an ordinary default the
    # subparser writes its own None over a --serial that was given before the
    # subcommand, silently discarding it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-s", "--serial", default=argparse.SUPPRESS,
                        help="adb serial, when several targets are attached")
    common.add_argument("-n", "--name", default=argparse.SUPPRESS,
                        help="rootfs instance to act on (also accepted before the subcommand)")

    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="report target capabilities and pick a strategy", parents=[common])
    p.add_argument("--raw", action="store_true", help="dump raw key=value facts")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("install", help="download a distro rootfs and install it on the target", parents=[common])
    p.add_argument("distro", nargs="?", default="debian",
                   help="distro[:release], e.g. debian:bookworm, ubuntu:noble (default: debian)")
    p.add_argument("--arch", help="distro architecture; detected from the target by default")
    p.add_argument("--size", default=config.DEFAULT_SIZE,
                   help=f"ext4 image size, sparse so this is a ceiling (default: {config.DEFAULT_SIZE})")
    p.add_argument("--hostname", default="androlinux", help="hostname inside the rootfs")
    p.add_argument("--redownload", action="store_true", help="ignore the host cache")
    p.add_argument("--permissive", action="store_true",
                   help="set SELinux permissive first. DANGEROUS on Samsung: Knox/RKP "
                        "panics the kernel and the device reboots. Off by default.")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("up", help="mount the rootfs and prepare it for use", parents=[common])
    p.add_argument("--hw", help=f"comma-separated /dev entries to expose "
                                f"(default: {','.join(container.DEFAULT_HW)})")
    p.add_argument("--permissive", action="store_true",
                   help="set SELinux permissive first. DANGEROUS on Samsung: Knox/RKP "
                        "panics the kernel and the device reboots. Off by default.")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("down", help="unmount the rootfs and release the loop device", parents=[common])
    p.set_defaults(func=cmd_down)

    p = sub.add_parser("resize", help="grow the rootfs image (sparse, so a bigger "
                                      "ceiling costs nothing until used)", parents=[common])
    p.add_argument("size", help="new size, e.g. 100G. Must be larger than the current one.")
    p.set_defaults(func=cmd_resize)

    p = sub.add_parser("remove", help="delete a rootfs; irreversible", parents=[common])
    p.add_argument("--all", action="store_true",
                   help="remove every instance, not just the selected one")
    p.add_argument("--yes", action="store_true", help="required; confirms the deletion")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("status", help="show what is installed and mounted", parents=[common])
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("enter", help="open an interactive shell inside the rootfs", parents=[common])
    p.add_argument("argv", nargs=argparse.REMAINDER, help="command to run instead of a login shell")
    p.add_argument("--user", help="enter as this user (default: the recorded one, if any)")
    p.add_argument("--root", action="store_true", help="enter as root even if a default user is set")
    p.set_defaults(func=cmd_enter)

    p = sub.add_parser("run", help="run one command inside the rootfs and print its output", parents=[common])
    p.add_argument("argv", nargs=argparse.REMAINDER, help="command and arguments")
    p.add_argument("--user", help="run as this user instead of root")
    p.add_argument("--timeout", type=float, default=900,
                   help="seconds to allow (default: 900; raise it for long builds)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("gui", help="manage the desktop session", parents=[common])
    p.add_argument("gui_action",
                   choices=("install", "start", "stop", "screenshot", "web", "viewer"))
    p.add_argument("--display", default=None,
                   help="X display; by default the one this instance already uses, "
                        "or the first free one")
    p.add_argument("--geometry", default=gui.DEFAULT_GEOMETRY,
                   help=f"session size (default: {gui.DEFAULT_GEOMETRY})")
    p.add_argument("-o", "--output", default="androlinux-desktop.png",
                   help="screenshot destination on the host")
    p.add_argument("--session", default=gui.DEFAULT_SESSION, choices=tuple(gui.SESSIONS),
                   help=f"desktop to install/start (default: {gui.DEFAULT_SESSION}). "
                        "'gnome' is Ubuntu's real desktop; heavier and needs no GPU it does "
                        "not have, so it may be slow.")
    p.add_argument("--session-user", default=None,
                   help="'gui start': run the desktop as this user; empty string forces root "
                        "(default: the instance's recorded user)")
    p.add_argument("--restart", action="store_true",
                   help="'gui start': replace a session that is already running. Without "
                        "this a live session is left alone, since replacing it destroys "
                        "whatever is open in it.")
    p.add_argument("--port", type=int, default=gui.DEFAULT_WEB_PORT,
                   help=f"port for 'gui web' (default: {gui.DEFAULT_WEB_PORT})")
    p.add_argument("--on-device", action="store_true",
                   help="'gui web': also open the device's own browser, putting the "
                        "Linux desktop on the phone screen")
    p.add_argument("--apk", help="'gui viewer': install this APK instead of downloading one")
    p.add_argument("--reinstall", action="store_true",
                   help="'gui viewer': reinstall even if already present")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("user", help="manage a non-root account inside the rootfs", parents=[common])
    p.add_argument("user_action", choices=("add", "status"), nargs="?", default="status")
    p.add_argument("username", nargs="?", help="account to create")
    p.add_argument("--uid", type=int, default=users.DEFAULT_UID, help="numeric uid")
    p.add_argument("--no-sudo", action="store_true", help="do not grant sudo")
    p.set_defaults(func=cmd_user)

    p = sub.add_parser("ssh", help="reach the rootfs over SSH", parents=[common])
    p.add_argument("ssh_action", choices=("enable", "status"), nargs="?", default="status")
    p.add_argument("--port", type=int, default=sshd.DEFAULT_PORT,
                   help=f"listen port (default: {sshd.DEFAULT_PORT})")
    p.add_argument("--user", help="account to allow (default: the recorded user)")
    p.add_argument("--key", help="public key to authorise (default: ~/.ssh/id_*.pub)")
    p.set_defaults(func=cmd_ssh)

    p = sub.add_parser("autostart", help="bring the rootfs up automatically after a reboot", parents=[common])
    p.add_argument("autostart_action", choices=("enable", "disable", "status"), nargs="?",
                   default="status")
    p.add_argument("--gui", action="store_true", help="also start the desktop at boot")
    p.add_argument("--geometry", default=gui.DEFAULT_GEOMETRY,
                   help=f"desktop size when started at boot (default: {gui.DEFAULT_GEOMETRY})")
    p.add_argument("--session", default=gui.DEFAULT_SESSION, choices=tuple(gui.SESSIONS),
                   help="desktop to start at boot")
    p.set_defaults(func=cmd_autostart)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # Must happen before the command runs: it rebinds the paths every module
        # reads, so selecting an instance afterwards would be too late.
        name = config.use(getattr(args, "name", None))
        if name != config.DEFAULT_INSTANCE:
            print(f"  instance: {name}  ({config.DEVICE_ROOT})")
        return args.func(args)
    except AdbError as exc:
        print(f"androlinux: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"androlinux: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
