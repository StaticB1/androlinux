"""The desktop session.

The Linux side gets its own X server (Xvnc) rather than drawing on the Android
display. Android's SurfaceFlinger owns the panel, the GPU and the input devices;
a second X server cannot take them without displacing Android. Rendering into a
framebuffer and serving it over RFB avoids that fight entirely, and the result is
reachable two ways with no extra work: from a VNC viewer on the device, or
forwarded to the host over adb — the same on an emulator and on a phone.

The session is unauthenticated on purpose, because it is reached through adb's
forwarder rather than over a network. :func:`start` states that in its output
instead of leaving it implied.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

from . import container, users
from .adb import Adb, AdbError

_SCRIPTS = Path(__file__).parent / "scripts"

#: Needed whichever desktop is chosen.
BASE_PACKAGES = (
    "tigervnc-standalone-server", "tigervnc-common",
    "dbus-x11", "x11-xserver-utils", "xterm", "fonts-dejavu-core",
)

#: The desktops this can start.
#:
#: "gnome" is Ubuntu's actual desktop — GNOME Shell with Yaru, the Ubuntu font and
#: the dock — rather than something merely themed to resemble it. Two caveats that
#: are properties of the environment, not of the packaging:
#:
#: * Ubuntu drives gnome-session through systemd *user* units, and an Android
#:   kernel has no PID namespace so there is no systemd. gnome-session has a
#:   built-in session manager to fall back on, but whether it does so cleanly here
#:   is an empirical question.
#: * There is no usable GPU (Adreno sits behind /dev/kgsl, which Mesa cannot
#:   drive), so GNOME Shell composites in software. XFCE tolerates that; GNOME
#:   Shell is much heavier.
#:
#: ubuntu-desktop-minimal is installed with --no-install-recommends deliberately:
#: the full metapackage pulls in snapd, and snapd requires systemd, so snaps
#: cannot work here at all. That is also why Firefox and Chromium — snap-only on
#: Ubuntu 24.04 — have to come from real .deb repositories.
SESSIONS = {
    "xfce": {
        "packages": (
            "xfce4-session", "xfwm4", "xfdesktop4", "xfce4-panel",
            "xfce4-terminal", "thunar",
        ),
        "command": "xfce4-session",
        "env": "",
        "proc": "xfwm4",
    },
    "gnome": {
        "packages": (
            "ubuntu-desktop-minimal", "gnome-session", "gnome-shell",
            "gnome-terminal", "nautilus", "gnome-control-center",
            "yaru-theme-gtk", "yaru-theme-icon", "yaru-theme-gnome-shell",
            "fonts-ubuntu", "gnome-shell-extension-ubuntu-dock",
        ),
        "command": "gnome-session --session=ubuntu",
        "proc": "gnome-shell",
        "env": (
            "export XDG_CURRENT_DESKTOP=ubuntu:GNOME\n"
            "export GNOME_SHELL_SESSION_MODE=ubuntu\n"
            "export XDG_SESSION_TYPE=x11\n"
            # Software rendering is all that is available; say so explicitly rather
            # than letting GNOME probe and fail.
            "export LIBGL_ALWAYS_SOFTWARE=1\n"
            "export MUTTER_DEBUG_FORCE_KMS_MODE=simple\n"
        ),
    },
}

DEFAULT_SESSION = "xfce"

#: Kept for callers that predate SESSIONS.
PACKAGES = BASE_PACKAGES + SESSIONS[DEFAULT_SESSION]["packages"]

DEFAULT_DISPLAY = ":1"
DEFAULT_GEOMETRY = "1280x800"
DEFAULT_DEPTH = "24"

#: Serves the session over HTTP so a browser can view it — including the browser
#: on the Android device itself, which is how the desktop appears on the phone
#: screen without installing any viewer app.
#:
#: /usr/bin/websockify comes from python3-websockify, not from the websockify
#: package (which ships only /usr/bin/rebind). The web assets come from novnc.
WEB_PACKAGES = ("novnc", "python3-websockify")
DEFAULT_WEB_PORT = 6080

#: The only browser on a google_apis emulator image.
ANDROID_BROWSER = "com.android.chrome"

#: A native on-device viewer, for when you would rather not look at the desktop
#: through a browser. AVNC is FOSS, ships an x86_64 RFB library, targets API 35,
#: and has no Google Play Services dependency — which matters because a
#: google_apis emulator image has no Play Store and no signed-in account.
#:
#: It registers a vnc:// VIEW intent, so it can be launched straight onto the
#: desktop with no tapping.
#:
#: The launch target is resolved at runtime rather than hardcoded. The activity
#: that *handles* the URI (AVNC's UriReceiverActivity) is not the one that ends up
#: in the foreground afterwards (its VncActivity), and naming the latter as the
#: launch target starts it with no connection details — the app then reports
#: "missing server info". Asking Android to resolve the intent avoids having to
#: know either name, and keeps this working with any other vnc:// handler.
VIEWER_PACKAGE = "com.gaurav.avnc"
VIEWER_URL = "https://f-droid.org/repo/com.gaurav.avnc_51.apk"
#: Checked before installing. An APK is executable code; fetching it over TLS says
#: nothing about which bytes arrived, so the hash is pinned here.
VIEWER_SHA256 = "e1a4a2c70f6d7ea5431c2f48e4c4aa711dc8dc99d7f733e6f5a2e4ee13111349"


def vnc_port(display: str) -> int:
    """RFB port for an X display number — :1 is 5901, by long convention."""
    try:
        return 5900 + int(display.lstrip(":"))
    except ValueError as exc:
        raise AdbError(f"bad display {display!r}; expected a form like ':1'") from exc


def recorded_display(adb: Adb) -> str | None:
    """The display this instance last used, if any."""
    from . import config
    res = adb.sh(f"cat {config.DEVICE_ROOT}/display 2>/dev/null", root=True, timeout=60)
    val = res.out.strip()
    return val if val.startswith(":") else None


def resolve_display(adb: Adb, explicit: str | None) -> str:
    """Decide which display this instance uses, and remember it.

    Instances share one network namespace, so they cannot share a display. The
    decision — including re-validating a remembered choice — lives in
    ``scripts/pick-display.sh`` so it happens in one device round trip.
    """
    from . import config

    script = config.preamble(
        ALX_ROOT=config.DEVICE_ROOT,
        ALX_MNT=config.MOUNT,
        ALX_EXPLICIT=explicit or "",
    ) + (_SCRIPTS / "pick-display.sh").read_text()

    res = adb.script(script, root=True, timeout=120, label="pick-display")
    chosen = None
    for line in res.lines():
        if line.startswith("display="):
            chosen = line.split("=", 1)[1].strip()
        elif line.startswith("note="):
            print(f"  {line.split('=', 1)[1].strip()}")
    if not chosen:
        raise AdbError(
            "could not choose a display:\n"
            f"{(res.err or res.out).strip() or '<no output>'}"
        )
    if chosen != DEFAULT_DISPLAY:
        print(f"  display {chosen} (port {vnc_port(chosen)})")
    return chosen


def install(adb: Adb, *, extra: tuple[str, ...] = (),
            session: str = DEFAULT_SESSION) -> None:
    """Install a desktop inside the rootfs. Idempotent."""
    if session not in SESSIONS:
        raise AdbError(f"unknown session {session!r}; choose from {', '.join(SESSIONS)}")
    pkgs = " ".join(BASE_PACKAGES + SESSIONS[session]["packages"] + extra)
    # `set -e` is required. container.run sets pipefail but not -e, so without it
    # the script's status is that of the trailing echo, which always succeeds — a
    # failed apt-get would report "installed: N packages" and exit 0.
    script = (
        "set -e\n"
        "export DEBIAN_FRONTEND=noninteractive\n"
        "apt-get update -qq\n"
        # --no-install-recommends keeps this near 400 packages instead of ~1200.
        f"apt-get install -y --no-install-recommends {pkgs}\n"
        "echo \"installed: $(dpkg -l | grep -cE '^ii') packages\"\n"
    )
    print(f"  installing the {session} desktop inside the rootfs "
          f"({len(BASE_PACKAGES + SESSIONS[session]['packages'])} metapackages; "
          f"this takes a while) ...")
    res = container.run(adb, script, timeout=3600)
    tail = "\n".join(res.lines()[-6:])
    print("\n".join(f"  {ln}" for ln in tail.splitlines()))
    if not res.ok:
        raise AdbError(f"desktop install failed (exit {res.code}):\n{res.err.strip()}")


def start(
    adb: Adb,
    *,
    display: str | None = None,
    geometry: str = DEFAULT_GEOMETRY,
    depth: str = DEFAULT_DEPTH,
    forward: bool = True,
    force: bool = False,
    user: str | None = None,
    session: str = DEFAULT_SESSION,
) -> int:
    """Start the desktop session and forward its port to the host.

    The session runs as ``user`` when one is given, or as the instance's recorded
    user when one exists; pass ``user=""`` to force root. Running the desktop as a
    human matters in practice — Chromium and Firefox refuse to start as root, and
    XFCE keeps its panel and session state in a real home directory.

    Returns the host port the session is reachable on.
    """
    display = resolve_display(adb, display)
    if user is None:
        user = users.default(adb) or ""
    _prepare_session(adb, user)

    if session not in SESSIONS:
        raise AdbError(f"unknown session {session!r}; choose from {', '.join(SESSIONS)}")
    spec = SESSIONS[session]

    body = (
        f"ALX_DISPLAY='{display}'\n"
        f"ALX_GEOMETRY='{geometry}'\n"
        f"ALX_DEPTH='{depth}'\n"
        f"ALX_FORCE='{1 if force else 0}'\n"
        f"ALX_SESSION_CMD='{spec['command']}'\n"
        f"ALX_SESSION_ENV='{spec['env']}'\n"
        f"ALX_SESSION_PROC='{spec['proc']}'\n"
        + (_SCRIPTS / "gui-start.sh").read_text()
    )
    res = container.run(adb, body, timeout=600, user=user or None)
    sys.stdout.write("\n".join(f"  {ln}" for ln in res.lines()) + "\n")
    if not res.ok:
        raise AdbError(f"could not start the desktop (exit {res.code}):\n{res.err.strip()}")
    if user:
        print(f"  session running as {user}")

    port = vnc_port(display)
    if forward:
        adb.forward(port, port).check(f"forwarding port {port}")
        print(f"  forwarded localhost:{port} → device:{port}")
        print(f"\n  connect a VNC viewer to  localhost:{port}")
        print("  the session has no password; it is reachable only through adb's forwarder\n")
    return port


def _prepare_session(adb: Adb, user: str) -> None:
    """The parts of session setup needing root, done before dropping to the user.

    dbus will not start without /etc/machine-id, and XFCE will not start without
    dbus — and nothing generates that file in a rootfs that has never run an init.
    Both that and /run/dbus are root's business; the session itself is not.
    """
    script = (
        "set -e\n"
        "if [ ! -s /etc/machine-id ]; then\n"
        "  if command -v dbus-uuidgen >/dev/null 2>&1; then\n"
        "    dbus-uuidgen > /etc/machine-id\n"
        "  else\n"
        "    tr -d - < /proc/sys/kernel/random/uuid > /etc/machine-id\n"
        "  fi\n"
        "  echo 'generated /etc/machine-id'\n"
        "fi\n"
        "mkdir -p /run/dbus /var/run/dbus /var/lib/dbus\n"
        # dbus reads its id from here, and refuses to start without one.
        "[ -e /var/lib/dbus/machine-id ] || ln -sf /etc/machine-id /var/lib/dbus/machine-id\n"
        #
        # The D-Bus *system* bus. Normally systemd starts dbus.service; with no
        # systemd nothing does, and GNOME Shell dies on it — accountsservice
        # reports "Couldn't connect to system bus" and main.js throws, leaving
        # gnome-shell defunct while gnome-session itself keeps running. XFCE never
        # noticed because it only needs the session bus that dbus-launch provides.
        "if ! pgrep -f 'dbus-daemon --system' >/dev/null 2>&1; then\n"
        "  dbus-daemon --system --fork && echo 'started the D-Bus system bus'\n"
        "else\n"
        "  echo 'D-Bus system bus already running'\n"
        "fi\n"
        # These are systemd units on a normal Ubuntu. GNOME asks them for the user
        # list and for privilege checks; without them the shell degrades or fails.
        "for svc in /usr/libexec/accounts-daemon /usr/lib/polkit-1/polkitd; do\n"
        "  [ -x \"$svc\" ] || continue\n"
        "  name=$(basename \"$svc\")\n"
        "  pgrep -x \"$name\" >/dev/null 2>&1 && continue\n"
        "  setsid \"$svc\" </dev/null >/dev/null 2>&1 &\n"
        "  echo \"started $name\"\n"
        "done\n"
        "sleep 2\n"
    )
    if user:
        # The session writes ~/.vnc and ~/.cache. A home left root-owned by an
        # earlier root-run session makes the user's session fail obscurely.
        script += (
            'h=$(getent passwd "$ALX_SESSION_USER" | cut -d: -f6)\n'
            'if [ -n "$h" ] && [ -d "$h" ]; then\n'
            '  chown -R "$ALX_SESSION_USER" "$h" || true\n'
            '  echo "home $h prepared for $ALX_SESSION_USER"\n'
            'fi\n'
        )
        script = f"ALX_SESSION_USER='{user}'\n" + script

    res = container.run(adb, script, timeout=300)
    for line in res.lines():
        print(f"  {line}")
    if not res.ok:
        raise AdbError(f"session preparation failed (exit {res.code}):\n{res.err.strip()}")


def stop(adb: Adb, *, display: str | None = None) -> None:
    """Kill the desktop session, its web proxy, and drop the port forwards."""
    display = display or recorded_display(adb) or DEFAULT_DISPLAY
    res = container.run(
        adb,
        f"vncserver -kill {display} 2>&1 || true\n"
        "pkill -f 'websockify --web' 2>/dev/null && echo 'stopped the web proxy' || true\n",
        timeout=180,
    )
    sys.stdout.write("\n".join(f"  {ln}" for ln in res.lines()) + "\n")

    for port in (vnc_port(display), DEFAULT_WEB_PORT):
        removed = adb.raw("forward", "--remove", f"tcp:{port}", timeout=30)
        # Only claim it if it happened; adb errors when no such forward exists.
        if removed.ok:
            print(f"  removed the forward on {port}")


def web(
    adb: Adb,
    *,
    display: str = DEFAULT_DISPLAY,
    port: int = DEFAULT_WEB_PORT,
    on_device: bool = False,
    forward: bool = True,
) -> str:
    """Serve the running session over HTTP, and optionally show it on the device.

    This is what puts the Linux desktop on the Android screen. The chroot shares
    Android's network namespace — androlinux uses no network namespace — so a
    listener bound inside the rootfs is reachable at the device's own loopback
    address, both by Android apps and by ``adb forward``.

    Returns the URL to open.
    """
    display = display or recorded_display(adb) or DEFAULT_DISPLAY
    if not container.is_up(adb):
        container.up(adb, quiet=True)

    pkgs = " ".join(WEB_PACKAGES)
    # Bound to 127.0.0.1 deliberately. Bare "6080" would bind 0.0.0.0, and
    # combined with the session's SecurityTypes=None that would expose an
    # unauthenticated desktop to anything able to reach the device's IP. Loopback
    # is sufficient for both consumers: Chrome runs on the device, and adb's
    # forwarder also connects from inside the device.
    script = (
        "set -e\n"
        "if ! command -v websockify >/dev/null 2>&1 || [ ! -f /usr/share/novnc/vnc.html ]; then\n"
        "  export DEBIAN_FRONTEND=noninteractive\n"
        "  apt-get update -qq\n"
        f"  apt-get install -y --no-install-recommends {pkgs}\n"
        "fi\n"
        "pkill -f 'websockify --web' 2>/dev/null || true\n"
        "sleep 1\n"
        # setsid and the redirects are required or adb waits on the proxy forever.
        f"setsid websockify --web /usr/share/novnc 127.0.0.1:{port} 127.0.0.1:{vnc_port(display)} "
        "</dev/null >/tmp/androlinux-websockify.log 2>&1 &\n"
        "sleep 4\n"
        # Prove it is actually serving rather than assuming the launch worked.
        f"grep -q ':{port}' /tmp/androlinux-websockify.log || true\n"
        f"curl -sf -o /dev/null -w 'http=%{{http_code}}\\n' http://127.0.0.1:{port}/vnc.html\n"
    )
    res = container.run(adb, script, timeout=1800)
    if not res.ok or "http=200" not in res.out:
        raise AdbError(
            f"could not serve the desktop over HTTP (exit {res.code}):\n"
            f"{res.out.strip()}\n{res.err.strip()}"
        )
    print(f"  serving noVNC on the device at 127.0.0.1:{port}")

    # 127.0.0.1 rather than 'localhost': websockify binds IPv4 only, while Android
    # resolves localhost to ::1 as well, so the name can cost a refused connection.
    url = f"http://127.0.0.1:{port}/vnc.html?autoconnect=1&resize=scale&reconnect=1"

    if forward:
        adb.forward(port, port).check(f"forwarding port {port}")
        print(f"  forwarded localhost:{port} → device:{port}")
        print(f"  from this host, open: {url}")

    if on_device:
        # One argument, so the remote shell parses the quotes itself — otherwise
        # adb's argv flattening lets the shell read '&' as a job separator and the
        # query string is silently truncated.
        launch = adb.sh(
            f"am start -a android.intent.action.VIEW -d '{url}'",
            root=True, timeout=120,
        )
        if not launch.ok:
            raise AdbError(f"could not open the browser on the device:\n{launch.err.strip()}")
        print(f"  opened {ANDROID_BROWSER} on the device — the desktop is now on the phone screen")

    return url


def _fetch_viewer_apk(*, quiet: bool = False) -> Path:
    """Download the viewer APK into the host cache, verifying its hash."""
    from . import config

    config.CACHE.mkdir(parents=True, exist_ok=True)
    dest = config.CACHE / "avnc.apk"

    if dest.exists() and _sha256(dest) == VIEWER_SHA256:
        if not quiet:
            print(f"  cached  {dest.name} (hash verified)")
        return dest

    if not quiet:
        print(f"  fetching {VIEWER_URL}")
    tmp = dest.with_suffix(".part")
    try:
        with urllib.request.urlopen(VIEWER_URL, timeout=120) as resp, open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh, length=1 << 20)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise AdbError(f"could not download the viewer: {exc}") from exc

    got = _sha256(tmp)
    if got != VIEWER_SHA256:
        tmp.unlink(missing_ok=True)
        raise AdbError(
            "the downloaded APK does not match its expected hash — refusing to install it.\n"
            f"  expected {VIEWER_SHA256}\n  got      {got}"
        )
    tmp.replace(dest)
    if not quiet:
        print(f"  downloaded {dest.name}, hash verified")
    return dest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_vnc_handler(adb: Adb, uri: str) -> str | None:
    """Ask Android which activity handles a ``vnc://`` URI.

    Returns a ``package/class`` component, or None if resolution fails — in which
    case the caller launches without an explicit component and lets Android decide.
    """
    res = adb.sh(
        f"cmd package resolve-activity -a android.intent.action.VIEW -d '{uri}'",
        root=True, timeout=60,
    )
    name = pkg = None
    for raw in res.out.splitlines():
        line = raw.strip()
        # The first block is the winning resolution; later blocks describe the
        # application object, so stop once both fields of the first are seen.
        if name is None and line.startswith("name="):
            name = line.removeprefix("name=").strip()
        elif pkg is None and line.startswith("packageName="):
            pkg = line.removeprefix("packageName=").strip()
        if name and pkg:
            break
    if not (name and pkg):
        return None
    return f"{pkg}/{name}"


def viewer(
    adb: Adb,
    *,
    display: str | None = None,
    apk: str | None = None,
    reinstall: bool = False,
) -> None:
    """Show the desktop on the device through a native app rather than a browser.

    The viewer connects over the device's own loopback to the session's RFB port,
    so — as with :func:`web` — no port forward or server change is needed. It gets
    a full-screen view with gesture input and an on-screen modifier bar, which a
    browser tab cannot offer.
    """
    display = display or recorded_display(adb) or DEFAULT_DISPLAY
    if not container.is_up(adb):
        container.up(adb, quiet=True)

    installed = adb.sh(f"pm list packages {VIEWER_PACKAGE}", timeout=60).out.strip()
    if reinstall or not installed:
        path = Path(apk) if apk else _fetch_viewer_apk()
        if not path.is_file():
            raise AdbError(f"no such APK: {path}")
        if not apk and _sha256(path) != VIEWER_SHA256:
            raise AdbError(f"{path} does not match the pinned hash; refusing to install")
        print(f"  installing {path.name} on the device")
        adb.raw("install", "-r", str(path), timeout=600).check("installing the viewer")
    else:
        print(f"  {VIEWER_PACKAGE} already installed")

    port = vnc_port(display)
    # 127.0.0.1 because the app runs on the device; nothing is forwarded here.
    uri = f"vnc://127.0.0.1:{port}"

    component = _resolve_vnc_handler(adb, uri)
    target = f" -n {component}" if component else ""
    if component:
        print(f"  {uri} is handled by {component}")

    launch = adb.sh(
        f"am start -a android.intent.action.VIEW -d '{uri}'{target}",
        root=True, timeout=120,
    )
    if not launch.ok:
        raise AdbError(f"could not launch the viewer:\n{launch.err.strip()}")

    resumed = adb.sh(
        "dumpsys activity activities | grep -m1 ResumedActivity", root=True, timeout=60
    ).out
    if VIEWER_PACKAGE not in resumed:
        print(f"  warning: launched, but the foreground activity is not {VIEWER_PACKAGE}")
        print(f"           currently: {resumed.strip() or 'unknown'}")
    else:
        print(f"  {VIEWER_PACKAGE} is in the foreground, connected to {uri}")
    print("  the session has no password, so it connects without prompting")
    print("  note: on first launch the app shows a short tutorial — dismiss it once")


def screenshot(adb: Adb, local_path: str, *, display: str | None = None,
               user: str | None = None) -> str:
    """Capture the running session to a PNG on the host.

    Captured inside the rootfs with the guest's own tools, because the host has no
    view of this X display at all.
    """
    from . import config

    display = display or recorded_display(adb) or DEFAULT_DISPLAY
    # Must run as whoever owns the session: X access is gated by the Xauthority
    # cookie in that user's home, so root — despite being root — gets "unable to
    # open display". Only surfaced once sessions stopped running as root.
    if user is None:
        user = users.default(adb) or ""

    # Every step is checked, because the obvious way to write this reports success
    # on an empty file: the redirect creates the file before the converter runs, and
    # with `ls` last the script exits 0 even when xwd and pnmtopng both failed.
    #
    # mktemp rather than a fixed name: /tmp is sticky, so a leftover root-owned
    # alx-shot.xwd from an earlier root-run session cannot be replaced or deleted
    # by an ordinary user, and the capture fails with "Permission denied".
    res = container.run(
        adb,
        "set -e\n"
        "if ! command -v xwd >/dev/null 2>&1 || ! command -v pnmtopng >/dev/null 2>&1; then\n"
        "  echo 'x11-apps and netpbm are needed; install them as root:' >&2\n"
        "  echo \"  androlinux run 'apt-get install -y x11-apps netpbm'\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "OUT=$(mktemp -u \"${TMPDIR:-/tmp}/alx-shot-XXXXXXXX\")\n"
        f"DISPLAY={display} xwd -root -silent > \"$OUT.xwd\"\n"
        "test -s \"$OUT.xwd\"\n"
        "xwdtopnm < \"$OUT.xwd\" | pnmtopng > \"$OUT.png\"\n"
        "test -s \"$OUT.png\"\n"
        "rm -f \"$OUT.xwd\"\n"
        "chmod 644 \"$OUT.png\"\n"
        "echo \"shot=$OUT.png\"\n",
        timeout=900,
        user=user or None,
    )
    if not res.ok:
        raise AdbError(
            f"screenshot failed (exit {res.code}).\n"
            f"  Is the session running? 'androlinux gui start' first.\n"
            f"{res.out.strip()}\n{res.err.strip()}"
        )

    remote = ""
    for line in res.lines():
        if line.startswith("shot="):
            remote = line.split("=", 1)[1].strip()
    if not remote:
        raise AdbError(f"the capture reported no output path:\n{res.out.strip()}")

    _pull_from_rootfs(adb, f"{config.MOUNT}{remote}", local_path)
    adb.sh(f"rm -f {config.MOUNT}{remote}", root=True, timeout=60)
    return local_path


def _pull_from_rootfs(adb: Adb, remote: str, local_path: str) -> None:
    """Copy a file out of the rootfs to the host.

    ``adb pull`` runs as the *shell* user, not as root. That is fine when root
    came from ``adb root`` (adbd itself is uid 0), but on a Magisk device root
    comes from ``su`` and adbd stays unprivileged — it cannot read anything under
    /data/androlinux. There, stage the file into /data/local/tmp as root and make
    it world-readable first.
    """
    if adb.root_method != "su":
        adb.pull(remote, local_path).check("pulling file")
        return

    from . import config
    staged = "/data/local/tmp/androlinux-pull.bin"
    adb.script(
        config.preamble(ALX_SRC=remote, ALX_DST=staged)
        + 'set -e\ncp "$ALX_SRC" "$ALX_DST"\nchmod 644 "$ALX_DST"\n',
        root=True, timeout=300, label="stage-pull",
    ).check("staging the file where adbd can read it")
    try:
        adb.pull(staged, local_path).check("pulling file")
    finally:
        adb.sh(f"rm -f {staged}", root=True, timeout=60)
