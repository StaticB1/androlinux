#!/bin/bash
# androlinux: start a desktop session. Runs *inside* the Debian rootfs.
#
# Injected above by gui.py:  ALX_DISPLAY  ALX_GEOMETRY  ALX_DEPTH
#
# The X server here is Xvnc: it renders into a framebuffer and serves it over
# RFB. That is deliberate rather than a compromise — the Android kernel owns the
# real display, and Android's SurfaceFlinger holds the GPU and input devices.
# Drawing to the panel directly would mean fighting SurfaceFlinger for them.
# Serving the session over a socket lets it be viewed on the device or forwarded
# to the host over adb, and it works identically on an emulator and a phone.
#
# There is no systemd here (the kernel has no PID namespace), so the session is
# started directly and daemonises itself.

set -eu

say() { printf '  %s\n' "$1"; }

command -v Xvnc >/dev/null 2>&1 || {
  echo "Xvnc not installed — run 'androlinux gui install' first" >&2; exit 1
}

# /etc/machine-id, /run/dbus and the D-Bus *system* bus are prepared by up.sh,
# which every start path runs — host, boot hook and on-device. They were once
# prepared by gui.py instead, which meant only the host path got them and starting
# from the tablet produced a session with no system bus. See FINDINGS K1.
#
# This is a guard, not the setup. Note it checks the *content*: Ubuntu ships
# /etc/machine-id containing the literal word "uninitialized" for systemd to
# replace on first boot, and with no systemd nothing replaces it. A `-s` test
# passes that happily, then D-Bus rejects it —
#   UUID file '/etc/machine-id' should contain a hex string of length 32
# — and the session dies with "Oh no! Something has gone wrong". See FINDINGS K2.
_mid=$(tr -d '[:space:]' < /etc/machine-id 2>/dev/null || echo "")
case "$_mid" in
  *[!0-9a-f]*|"") _mid_ok=no ;;
  *) [ ${#_mid} -eq 32 ] && _mid_ok=yes || _mid_ok=no ;;
esac
if [ "$_mid_ok" != yes ]; then
  echo "/etc/machine-id is not a valid 32-char hex UUID (got '${_mid:-<empty>}')." >&2
  echo "dbus will refuse to start, and so will the desktop." >&2
  echo "Run 'androlinux up' to regenerate it." >&2
  exit 1
fi

mkdir -p "$HOME/.vnc"

# TigerVNC runs this instead of a window manager. exec matters: without it the
# script exits, and Xvnc tears the session down when its startup process ends.
#
# The session command and its environment are injected rather than hardcoded, so
# the same script can start XFCE or GNOME. Note the heredoc is *unquoted* here so
# ALX_SESSION_* expand now, while \$HOME is escaped to survive into the file.
cat > "$HOME/.vnc/xstartup" <<EOF
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
$ALX_SESSION_ENV
[ -r "\$HOME/.Xresources" ] && xrdb "\$HOME/.Xresources"
exec dbus-launch --exit-with-session $ALX_SESSION_CMD
EOF
chmod 755 "$HOME/.vnc/xstartup"
say "session command: $ALX_SESSION_CMD"

# Clean up a session left behind by a hard shutdown; a stale lock makes the new
# server refuse the display number.
#
# The socket must not be removed until the old server is confirmed gone.
# `vncserver -kill` locates its target through $HOME/.vnc/$(hostname):N.pid, and
# that pidfile does go stale in practice ("Cleaning stale pidfile" appears in the
# logs on this setup). If the kill misses and we unlink the socket anyway, a live
# Xtigervnc keeps TCP 5901, the replacement cannot bind, and the caller waits out
# the full 45s poll below before failing — with the original session now
# unreachable over its socket as well.
# Kill the session's *components*, not just its X server, before starting a new
# one. `vncserver -kill` stops Xvnc and nothing else: gnome-session, the session
# dbus-daemon and the keyring daemons are reparented and survive it. They then
# hold D-Bus names the next session needs —
#   tracker-miner-fs-3: Could not request DBus name ... already taken
# — so gnome-shell exits and the user gets a black screen or "Oh no! Something has
# gone wrong", with a perfectly healthy Xvnc in front of it.
#
# Named explicitly rather than `pkill -u`, because the session user may also own an
# ssh login or a running build that must not be killed. Note this runs AS the
# session user, so pkill cannot reach the root-owned D-Bus *system* bus even by
# accident — but the guard is here in case the session ever runs as root.
kill_session_components() {
  for _p in gnome-shell gnome-session-binary gnome-settings-daemon gsd-media-keys \
            xdg-desktop-portal xdg-desktop-portal-gnome xdg-permission-store \
            gnome-keyring-daemon tracker-miner-fs-3 tracker-extract-3 \
            evolution-source-registry evolution-calendar-factory \
            evolution-addressbook-factory update-notifier at-spi-bus-launcher \
            xfce4-session xfwm4 xfce4-panel xfdesktop dbus-launch; do
    pkill -x "$_p" 2>/dev/null || true
  done
  # Session buses only. Never as root, or this would take out the system bus.
  if [ "$(id -u)" != "0" ]; then
    pkill -x dbus-daemon 2>/dev/null || true
  fi
  sleep 2
}

N=${ALX_DISPLAY#:}
PORT=$((5900 + N))

# Liveness is decided by connecting to the RFB port, not by looking for the
# process. `pgrep` cannot be trusted here: Android mounts /proc with hidepid=2
# (verified on an SM-T970 — `proc /proc proc rw,relatime,gid=3009,hidepid=2`), so
# a session running as one user is completely invisible to another. Once the
# desktop started running as a normal user while this check ran as root, pgrep
# reported nothing and the script cheerfully tried to start a second server on a
# port already in use. A TCP connect works regardless of uid.
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$PORT") >/dev/null 2>&1; }

# Whose session is it? The X socket lives inside *this* rootfs, so its presence
# distinguishes "ours" from "another instance's". Both instances share one network
# namespace, so they genuinely compete for the port.
ours() { [ -e "/tmp/.X11-unix/X$N" ]; }

if port_open && ! ours; then
  echo "display $ALX_DISPLAY (port $PORT) is held by something outside this rootfs," >&2
  echo "most likely another androlinux instance sharing the network namespace." >&2
  echo "Give this one its own display, e.g.  androlinux gui start --display :2" >&2
  exit 1
fi

# A LIVE session of our own is left alone. This is the difference between "start"
# and "restart", and getting it wrong is destructive: the home-screen widget runs
# this every tap, so unconditionally replacing the server tore down whatever the
# user had open and dropped the connected viewer — indistinguishable from the app
# crashing. Only an explicit ALX_FORCE=1 replaces a healthy session.
if port_open && ours; then
  if [ "${ALX_FORCE:-0}" != "1" ]; then
    say "session already running on $ALX_DISPLAY — left untouched"
    exit 0
  fi
  say "replacing the running session on $ALX_DISPLAY (forced)"
  vncserver -kill "$ALX_DISPLAY" >/dev/null 2>&1 || true

  i=0
  while [ $i -lt 10 ]; do
    port_open || break
    sleep 1
    i=$((i + 1))
  done

  if port_open; then
    # vncserver -kill missed it — its pidfile goes stale, and a session started by
    # a different user is invisible to it. Try directly before giving up.
    say "vncserver -kill did not stop it; signalling Xtigervnc directly"
    pkill -x Xtigervnc 2>/dev/null || true
    i=0
    while [ $i -lt 8 ]; do
      port_open || break
      sleep 1
      i=$((i + 1))
    done
  fi

  if port_open; then
    echo "an X server still holds $ALX_DISPLAY (port $PORT);" >&2
    echo "refusing to unlink its socket — it may belong to another user." >&2
    echo "Find it with:  androlinux run 'ps -eo user,pid,comm | grep Xtigervnc'" >&2
    exit 1
  fi
  kill_session_components
fi

# A socket with nothing listening behind it is genuinely stale — from a hard
# shutdown or a reboot — and must go, or the new server refuses the display.
if [ -e "/tmp/.X11-unix/X$N" ] || [ -e "/tmp/.X$N-lock" ]; then
  rm -f "/tmp/.X$N-lock" "/tmp/.X11-unix/X$N" 2>/dev/null || true
  say "cleared a stale session on $ALX_DISPLAY"
fi

_leftovers=$(pgrep -x gnome-session-binary 2>/dev/null | wc -l)
if [ "${_leftovers:-0}" -gt 0 ] || pgrep -x dbus-launch >/dev/null 2>&1; then
  say "clearing leftover session processes from a previous run"
  kill_session_components
fi

# setsid is load-bearing, not decoration. `adb shell` waits for the whole
# process group to exit, so a surviving X server keeps the adb call hanging
# forever even though the session started correctly. setsid puts the server in
# its own session, and </dev/null plus the log redirect releases every inherited
# descriptor, letting adb return.
#
# The server binds loopback only (-localhost, TigerVNC's default). adb's
# forwarder runs on the device and connects to 127.0.0.1, so loopback is
# sufficient to reach the session from the host — and it means the desktop is
# never exposed on the device's Wi-Fi or mobile interfaces. Binding 0.0.0.0
# instead would also make TigerVNC refuse an unauthenticated session outright,
# which is a fair objection on its part.
setsid vncserver "$ALX_DISPLAY" \
  -geometry "$ALX_GEOMETRY" \
  -depth "$ALX_DEPTH" \
  -SecurityTypes None \
  -localhost \
  </dev/null >/tmp/androlinux-vnc.log 2>&1 &

# Poll for the display socket rather than sleeping a fixed guess: a cold start
# with fontconfig cache generation is much slower than a warm one.
N=${ALX_DISPLAY#:}
up=no
for _ in $(seq 1 45); do
  if [ -e "/tmp/.X11-unix/X$N" ]; then up=yes; break; fi
  sleep 1
done

if [ "$up" != yes ]; then
  echo "the X server never created /tmp/.X11-unix/X$N:" >&2
  tail -25 /tmp/androlinux-vnc.log >&2
  exit 1
fi

# TigerVNC's server binary is Xtigervnc; matching only "Xvnc" silently misses it.
if pgrep -x Xtigervnc >/dev/null 2>&1 || pgrep -x Xvnc >/dev/null 2>&1; then
  say "X server up on $ALX_DISPLAY ($ALX_GEOMETRY, depth $ALX_DEPTH)"
else
  echo "the display socket exists but no X server is running:" >&2
  tail -25 /tmp/androlinux-vnc.log >&2
  exit 1
fi

# The compositor comes up a moment after the server does. Which process to wait
# for depends on the session — checking for xfwm4 while starting GNOME reported a
# failure that had not happened, and hid a real one that had.
for _ in $(seq 1 40); do
  pgrep -x "$ALX_SESSION_PROC" >/dev/null 2>&1 && break
  sleep 1
done
if pgrep -x "$ALX_SESSION_PROC" >/dev/null 2>&1; then
  say "session running ($ALX_SESSION_PROC up)"
else
  say "warning: X is up but $ALX_SESSION_PROC is not running"
  say "check the session log:  androlinux run --user <user> 'tail -40 \$HOME/.vnc/*.log'"
fi
