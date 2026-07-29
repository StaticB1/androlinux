# androlinux: start everything from the device itself, with no host attached.
#
# Injected above by autostart.py:  ALX_ROOT ALX_VNC_URI
#
# This is what you run from a terminal app on the device — Termux, say:
#
#     su -c 'sh /data/androlinux/start'
#
# It is the same work the boot hook does, plus opening the viewer, and it is
# safe to run when things are already up: up.sh is idempotent and the desktop
# start clears a stale session first.
#
# Output goes to the terminal here rather than only to a log, because unlike the
# boot hook there is somebody watching.

set -u

echo "androlinux: starting"

if [ ! -x "$ALX_ROOT/boot.sh" ]; then
  echo "  $ALX_ROOT/boot.sh is missing — run 'androlinux autostart enable' from the host once" >&2
  exit 1
fi

# Short-circuit when everything is already up. This is the common case: the home
# screen widget runs this on every tap, and most taps happen when the desktop is
# already there and the user simply wants to look at it again. Doing anything
# more than raising the viewer here risks disturbing a working session.
if grep -q " $ALX_ROOT/mnt " /proc/mounts && pgrep -x Xtigervnc >/dev/null 2>&1; then
  echo "  already running — opening the viewer"
  am start -a android.intent.action.VIEW -d "$ALX_VNC_URI" >/dev/null 2>&1 \
    && echo "androlinux: ready" \
    || echo "  viewer: could not open — start it from the app drawer"
  exit 0
fi

# boot.sh mounts the rootfs and starts the desktop, logging as it goes.
sh "$ALX_ROOT/boot.sh"

# Report what actually came up rather than assuming it did.
if grep -q " $ALX_ROOT/mnt " /proc/mounts; then
  echo "  rootfs: mounted"
else
  echo "  rootfs: NOT mounted — see $ALX_ROOT/boot.log" >&2
  exit 1
fi

i=0
while [ $i -lt 30 ]; do
  pgrep -x Xtigervnc >/dev/null 2>&1 && break
  sleep 1
  i=$((i + 1))
done

if pgrep -x Xtigervnc >/dev/null 2>&1; then
  echo "  desktop: running"
else
  echo "  desktop: did not start — see $ALX_ROOT/boot.log" >&2
  exit 1
fi

# Bring the viewer to the front. No -n component: the activity that handles the
# vnc:// URI is not the one that ends up in the foreground, and naming the latter
# starts it with no connection details ("missing server info").
am start -a android.intent.action.VIEW -d "$ALX_VNC_URI" >/dev/null 2>&1 \
  && echo "  viewer: opened $ALX_VNC_URI" \
  || echo "  viewer: could not open — start it from the app drawer"

echo "androlinux: ready"
