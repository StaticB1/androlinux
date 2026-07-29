# Choose the X display for this instance. Runs on the target as uid 0.
#
# Injected above by gui.py:  ALX_ROOT ALX_MNT ALX_EXPLICIT
#
# Instances share one network namespace, so they cannot share a display — the
# second server finds the RFB port taken. An instance therefore gets a display
# once and remembers it in $ALX_ROOT/display.
#
# A remembered choice is *re-validated* rather than trusted. An earlier version
# recorded a display it never managed to start on, and every later run then
# short-circuited onto that same broken choice. A stored value is only good if the
# port is free, or busy because of our own server.
#
# Note this must be POSIX sh: it runs in Android's mksh, which has no /dev/tcp.
# Port liveness is asked of `ss` instead, which is also the more reliable
# question — listening sockets are visible across instances, while /proc is
# mounted hidepid=2 and hides other users' processes entirely.

set -u

LISTENING=$(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null || echo "")

# usable <n> — display :n is either free, or held by this instance's own server.
usable() {
  _n=$1
  _port=$((5900 + _n))
  if ! printf '%s\n' "$LISTENING" | grep -qE ":$_port([^0-9]|\$)"; then
    return 0
  fi
  # Busy. Ours only if the X socket exists inside *this* rootfs.
  [ -e "$ALX_MNT/tmp/.X11-unix/X$_n" ] && return 0
  return 1
}

CHOICE=""

if [ -n "${ALX_EXPLICIT:-}" ]; then
  # An explicit request is honoured even if busy: gui-start.sh reports the
  # collision with a better message than this script could.
  CHOICE=$(printf '%s' "$ALX_EXPLICIT" | tr -d ':')
else
  REMEMBERED=$(tr -d ':\n' < "$ALX_ROOT/display" 2>/dev/null || echo "")
  if [ -n "$REMEMBERED" ] && usable "$REMEMBERED"; then
    CHOICE="$REMEMBERED"
  elif [ -n "$REMEMBERED" ]; then
    printf 'note=stored display :%s is taken by another instance, re-picking\n' "$REMEMBERED"
  fi

  if [ -z "$CHOICE" ]; then
    for n in 1 2 3 4 5 6 7 8 9 10; do
      if usable "$n"; then CHOICE="$n"; break; fi
    done
  fi
fi

if [ -z "$CHOICE" ]; then
  echo "error=displays :1 through :10 are all in use on this device" >&2
  exit 1
fi

mkdir -p "$ALX_ROOT"
printf ':%s\n' "$CHOICE" > "$ALX_ROOT/display"
printf 'display=:%s\n' "$CHOICE"
