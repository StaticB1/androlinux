# androlinux boot hook — runs on the target as uid 0, early in Android's boot.
#
# Injected above by autostart.py:  ALX_ROOT ALX_IMG ALX_START_GUI
#
# Two things make this different from running `androlinux up` by hand.
#
# It runs before /data is necessarily usable. On a device with file-based
# encryption, /data is not readable until the user unlocks the device the first
# time after a reboot, and Magisk's service.d fires well before that. So this
# waits for the rootfs image to appear rather than assuming it, and gives up
# quietly instead of failing loudly at a moment when nobody is watching.
#
# It has no controlling terminal and nowhere to print. Everything is appended to
# a log, because a boot hook that writes to stdout writes to nothing.

LOG="$ALX_ROOT/boot.log"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo '?')" "$1" >> "$LOG"; }

log "boot hook started"

# Wait up to ~4 minutes for /data to become readable and the image to appear.
i=0
while [ ! -f "$ALX_IMG" ] && [ "$i" -lt 120 ]; do
  sleep 2
  i=$((i + 1))
done

if [ ! -f "$ALX_IMG" ]; then
  log "gave up: $ALX_IMG never appeared (device may still be locked, or nothing is installed)"
  exit 0
fi
log "rootfs image present after ${i}0s"

if [ ! -x "$ALX_ROOT/up.sh" ]; then
  log "no $ALX_ROOT/up.sh — re-run 'androlinux autostart enable'"
  exit 0
fi

sh "$ALX_ROOT/up.sh" >> "$LOG" 2>&1
if [ $? -eq 0 ]; then
  log "rootfs up"
else
  log "up.sh failed — see above"
  exit 0
fi

if [ "$ALX_START_GUI" = "1" ]; then
  if [ -x "$ALX_ROOT/chroot-exec" ]; then
    # setsid, or the session's X server keeps this hook alive forever and Android
    # waits on it during boot.
    setsid sh "$ALX_ROOT/chroot-exec" /bin/bash /usr/local/sbin/androlinux-gui-start \
      </dev/null >> "$LOG" 2>&1 &
    log "desktop session starting in the background"
  else
    log "cannot start the desktop: $ALX_ROOT/chroot-exec is missing"
  fi
fi

log "boot hook done"
