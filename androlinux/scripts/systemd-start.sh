# androlinux: boot the distro's systemd as PID 1. Runs on the target as uid 0.
#
# Injected above by systemd.py:  ALX_ROOT ALX_MNT
#
# This is the piece that needs a rebuilt kernel. systemd checks getpid() == 1 and
# exits if it is not PID 1, so it needs a PID namespace — and Android's GKI
# configuration disables that (`# CONFIG_PID_NS is not set`). With a kernel built
# with CONFIG_PID_NS=y, `unshare --pid --fork` makes our child PID 1 inside a new
# namespace and systemd starts normally, still on the Android kernel and still
# without virtualisation.
#
# Note this is NOT how the rest of androlinux runs things: `up`/`run`/`enter` use
# a plain chroot in Android's own namespaces. systemd is the one component that
# requires the extra isolation, so it gets its own namespace and everything else
# joins that namespace afterwards via nsenter.

set -eu

say() { printf '  %s\n' "$1"; }

PIDFILE="$ALX_ROOT/systemd.pid"
LOG="$ALX_ROOT/systemd.log"

# ------------------------------------------------------- is it already running

if [ -f "$PIDFILE" ]; then
  old=$(cat "$PIDFILE" 2>/dev/null || echo "")
  if [ -n "$old" ] && [ -d "/proc/$old" ]; then
    # Confirm it is really our systemd and not a recycled pid.
    if [ "$(readlink "/proc/$old/root" 2>/dev/null)" = "$ALX_MNT" ]; then
      say "systemd already running as pid $old (PID 1 inside its namespace)"
      exit 0
    fi
  fi
  rm -f "$PIDFILE"
fi

# ------------------------------------------------------------ preconditions

[ -x "$ALX_MNT/lib/systemd/systemd" ] || [ -x "$ALX_MNT/usr/lib/systemd/systemd" ] || {
  echo "systemd is not installed in the rootfs" >&2
  echo "  install it with: androlinux run 'apt-get install -y systemd'" >&2
  exit 1
}

# Fail with a precise diagnosis rather than letting systemd exit obscurely.
if ! unshare --pid --fork true 2>/dev/null; then
  echo "this kernel will not create a PID namespace." >&2
  echo "  CONFIG_PID_NS is disabled in Android's GKI configuration, so systemd" >&2
  echo "  cannot be PID 1. Build a kernel with it enabled:" >&2
  echo "    kernel/build-kernel.sh   then boot with  emulator -kernel <bzImage>" >&2
  exit 1
fi
say "kernel provides PID namespaces"

# systemd (and dbus) refuse to work without a machine id, and nothing generates
# one in a rootfs that has never run an init.
if [ ! -s "$ALX_MNT/etc/machine-id" ]; then
  chroot "$ALX_MNT" /bin/sh -c 'command -v dbus-uuidgen >/dev/null 2>&1 \
      && dbus-uuidgen > /etc/machine-id \
      || cat /proc/sys/kernel/random/uuid | tr -d "-" > /etc/machine-id'
  say "generated /etc/machine-id"
fi

# systemd wants these to exist and be writable; a rootfs from a tarball has them
# as plain empty directories.
mkdir -p "$ALX_MNT/run" "$ALX_MNT/run/lock" "$ALX_MNT/var/log" "$ALX_MNT/tmp"
chmod 1777 "$ALX_MNT/tmp"

# The chroot's own /proc must be mounted *inside* the new PID namespace, or
# systemd sees Android's process list and immediately concludes it is not PID 1.
# Unmount any /proc left from a plain `up` so the namespace mounts a fresh one.
if grep -q " $ALX_MNT/proc " /proc/mounts; then
  umount "$ALX_MNT/proc" 2>/dev/null || umount -l "$ALX_MNT/proc" 2>/dev/null || true
fi

INIT=/lib/systemd/systemd
[ -x "$ALX_MNT/lib/systemd/systemd" ] || INIT=/usr/lib/systemd/systemd

# ------------------------------------------------------------------- launch
# setsid detaches it from this adb session, and </dev/null plus the log redirect
# releases every inherited descriptor — otherwise `adb shell` waits on the
# running init forever.
#
# --mount gives the namespace a private mount table so mounting /proc here does
# not disturb Android's. --fork is required: unshare itself cannot become PID 1,
# only its child can.

setsid unshare --pid --mount --fork sh -c "
  mount -t proc proc '$ALX_MNT/proc' || exit 91
  exec chroot '$ALX_MNT' $INIT --system
" </dev/null >>"$LOG" 2>&1 &

# --------------------------------------------------------------- find its pid
# $! is the setsid wrapper, not systemd. Locate the process that is actually
# running as init inside our rootfs.

found=""
for _ in $(seq 1 30); do
  sleep 1
  for pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
    [ "$(readlink "/proc/$pid/root" 2>/dev/null)" = "$ALX_MNT" ] || continue
    case "$(cat "/proc/$pid/comm" 2>/dev/null || echo)" in
      systemd)
        # Inside its own namespace this process is PID 1; from Android it is $pid.
        found="$pid"
        break
        ;;
    esac
  done
  [ -n "$found" ] && break
done

if [ -z "$found" ]; then
  echo "systemd did not come up. Last log lines:" >&2
  tail -25 "$LOG" >&2
  exit 1
fi

printf '%s\n' "$found" > "$PIDFILE"
say "systemd started (pid $found on the Android kernel, PID 1 in its namespace)"

# Give it a moment to reach a steady state before reporting.
sleep 3
if [ -d "/proc/$found" ]; then
  say "still running after 3s"
else
  echo "systemd exited immediately. Last log lines:" >&2
  tail -25 "$LOG" >&2
  exit 1
fi
