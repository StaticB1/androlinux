# androlinux: bring the Linux rootfs up. Runs on the target as uid 0, idempotent.
#
# Injected above by container.py:
#   ALX_ROOT ALX_IMG ALX_MNT ALX_HW
#
# `up` mounts the ext4 image and gives the rootfs the kernel interfaces a distro
# expects. Nothing here emulates anything: /proc and /sys are this kernel's own,
# and processes in the rootfs are ordinary processes on the Android kernel.
#
# /dev is built fresh as a tmpfs with explicit device nodes rather than
# bind-mounted from Android. Bind-mounting would mean every mkdir the distro does
# under /dev lands in Android's live /dev, and would hand the rootfs every node
# on the device. Building it explicitly makes hardware exposure a decision
# ($ALX_HW) instead of an accident.

set -eu

say() { printf '  %s\n' "$1"; }
mounted() { grep -q " $1 " /proc/mounts; }

[ -f "$ALX_IMG" ] || { echo "no rootfs image at $ALX_IMG — run 'androlinux install'" >&2; exit 1; }

# SELinux is left alone by default, and this is not caution for its own sake:
# on a Samsung device `setenforce 0` PANICS THE KERNEL. Knox/RKP treats it as
# tampering and reboots on the spot. Verified on an SM-T970, where a log synced
# either side of the call contains "pre=Enforcing" and nothing after it.
#
# It is usually unnecessary anyway. Magisk's su runs in an unconfined domain, so
# mount and mknod succeed while enforcing — the probe tests exactly that. Only if
# the rootfs misbehaves under enforcing is ALX_SETENFORCE=1 worth trying, and on
# Samsung it should not be tried at all.
MODE=$(getenforce 2>/dev/null || echo unknown)
if [ "${ALX_SETENFORCE:-0}" = "1" ] && [ "$MODE" = "Enforcing" ]; then
  say "setting SELinux permissive (requested)"
  setenforce 0 && say "SELinux is now $(getenforce 2>/dev/null)"
else
  say "SELinux left as-is ($MODE)"
fi

# ------------------------------------------------------------------ the image

mkdir -p "$ALX_MNT"
if mounted "$ALX_MNT"; then
  say "rootfs already mounted"
else
  mount -o loop,rw,suid,dev,exec "$ALX_IMG" "$ALX_MNT"
  say "rootfs mounted ($(grep " $ALX_MNT " /proc/mounts | cut -d' ' -f4))"
fi

# ----------------------------------------------------------------- resolv.conf
# Rewritten on every `up`, not just at install: a device changes networks, and a
# resolv.conf captured once goes stale. Android's own resolvers are preferred
# when it publishes them, since a network may use split-horizon or captive DNS
# that public resolvers cannot answer.

_resolv="$ALX_MNT/etc/resolv.conf"
[ -L "$_resolv" ] && rm -f "$_resolv"
: > "$_resolv"
for _p in net.dns1 net.dns2 net.dns3 net.dns4; do
  _v=$(getprop "$_p" 2>/dev/null)
  case "$_v" in
    ""|0.0.0.0) ;;
    *) printf 'nameserver %s\n' "$_v" >> "$_resolv" ;;
  esac
done
if [ ! -s "$_resolv" ]; then
  for _v in ${ALX_DNS:-1.1.1.1 8.8.8.8}; do printf 'nameserver %s\n' "$_v" >> "$_resolv"; done
  say "resolv.conf: Android published no DNS, using ${ALX_DNS:-1.1.1.1 8.8.8.8}"
else
  say "resolv.conf: $(tr '\n' ' ' < "$_resolv" | sed 's/nameserver //g')"
fi

# --------------------------------------------------------------- kernel fs

mkdir -p "$ALX_MNT/proc" "$ALX_MNT/sys" "$ALX_MNT/dev" "$ALX_MNT/run" "$ALX_MNT/tmp"
chmod 1777 "$ALX_MNT/tmp"

mounted "$ALX_MNT/proc" || mount -t proc proc "$ALX_MNT/proc"
mounted "$ALX_MNT/sys"  || mount -t sysfs sysfs "$ALX_MNT/sys"
mounted "$ALX_MNT/run"  || mount -t tmpfs -o mode=755,nosuid,nodev tmpfs "$ALX_MNT/run"

# ------------------------------------------------------------------- /dev

if ! mounted "$ALX_MNT/dev"; then
  mount -t tmpfs -o mode=755,size=64M tmpfs "$ALX_MNT/dev"

  # name         type major minor mode
  for spec in "null c 1 3 666" "zero c 1 5 666" "full c 1 7 666" \
              "random c 1 8 666" "urandom c 1 9 666" "tty c 5 0 666" \
              "console c 5 1 622" "ptmx c 5 2 666" "kmsg c 1 11 600"; do
    set -- $spec
    mknod "$ALX_MNT/dev/$1" "$2" "$3" "$4" 2>/dev/null && chmod "$5" "$ALX_MNT/dev/$1"
  done

  mkdir -p "$ALX_MNT/dev/pts" "$ALX_MNT/dev/shm"
  # gid=5 is the tty group in Debian; without it terminal allocation fails.
  mount -t devpts -o nosuid,noexec,gid=5,mode=620,ptmxmode=666 devpts "$ALX_MNT/dev/pts"
  mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs "$ALX_MNT/dev/shm"

  ln -sf /proc/self/fd   "$ALX_MNT/dev/fd"
  ln -sf /proc/self/fd/0 "$ALX_MNT/dev/stdin"
  ln -sf /proc/self/fd/1 "$ALX_MNT/dev/stdout"
  ln -sf /proc/self/fd/2 "$ALX_MNT/dev/stderr"
  say "/dev built (tmpfs + explicit nodes, devpts, shm)"
fi

# --------------------------------------------------------------- hardware
# Only what was asked for, bound in from Android's /dev. Absent nodes are
# reported rather than skipped silently, since "no sound" should not be a
# mystery later.

# Device *files* cannot be bind-mounted here: Android's toybox mount sees a
# non-directory source and tries to attach it as a loop device, failing with
# "losetup: Invalid argument". Recreating the node with mknod at the same
# major/minor is both what works and what a real /dev would contain. Directories
# (/dev/snd, /dev/dri, /dev/input) still need a bind, and that does work.
expose_dev() {
  _src="/dev/$1"
  _dst="$ALX_MNT/dev/$1"

  if [ ! -e "$_src" ]; then
    say "hardware: /dev/$1 absent on this kernel — not exposed"
    return 0
  fi

  if [ -d "$_src" ]; then
    mkdir -p "$_dst"
    mounted "$_dst" && return 0
    mount --bind "$_src" "$_dst" 2>/dev/null \
      && say "hardware: /dev/$1 exposed (bind)" \
      || say "hardware: /dev/$1 could not be bound"
    return 0
  fi

  if [ -e "$_dst" ]; then
    return 0
  fi

  _type=c
  [ -b "$_src" ] && _type=b
  # toybox stat reports major/minor in hex; mknod wants decimal.
  _maj=$(printf '%d' "0x$(stat -c %t "$_src")" 2>/dev/null || echo "")
  _min=$(printf '%d' "0x$(stat -c %T "$_src")" 2>/dev/null || echo "")
  if [ -z "$_maj" ] || [ -z "$_min" ]; then
    say "hardware: /dev/$1 — could not read its device numbers"
    return 0
  fi
  if mknod "$_dst" "$_type" "$_maj" "$_min" 2>/dev/null; then
    chmod 666 "$_dst"
    say "hardware: /dev/$1 exposed ($_type $_maj:$_min)"
  else
    say "hardware: /dev/$1 — mknod failed"
  fi
}

for hw in $ALX_HW; do
  expose_dev "$hw"
done

# cgroup v2 is present on Android 12+. Exposing it read-write lets the distro's
# tooling see resource accounting, though Android owns the hierarchy.
if [ -e /sys/fs/cgroup/cgroup.controllers ] && [ ! -e "$ALX_MNT/sys/fs/cgroup/cgroup.controllers" ]; then
  mkdir -p "$ALX_MNT/sys/fs/cgroup"
  mount --bind /sys/fs/cgroup "$ALX_MNT/sys/fs/cgroup" 2>/dev/null \
    && say "cgroup v2 exposed" || true
fi

# ------------------------------------------------------- chroot entry helper
# Written on every `up` so it always matches the installed tool version. This is
# what `androlinux enter` and `androlinux run` invoke; keeping it on the device
# means the interactive path needs no quoting through adb.

cat > "$ALX_ROOT/chroot-exec" <<EOF
#!/system/bin/sh
# Enter the androlinux rootfs. Args are the command to run; default is a login shell.
exec chroot "$ALX_MNT" /usr/bin/env -i \\
  HOME=/root \\
  USER=root \\
  TERM="\${TERM:-xterm-256color}" \\
  LANG=C.UTF-8 \\
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \\
  ANDROLINUX=1 \\
  "\$@"
EOF
chmod 755 "$ALX_ROOT/chroot-exec"

# sshd, if it was enabled. /run is a fresh tmpfs on every `up`, so its
# privilege-separation directory has to be recreated each time — this cannot be a
# one-off step at enable time.
if [ -f "$ALX_MNT/etc/androlinux-ssh" ]; then
  mkdir -p "$ALX_MNT/run/sshd"
  chmod 0755 "$ALX_MNT/run/sshd"
  if [ -x "$ALX_ROOT/chroot-exec" ]; then
    setsid "$ALX_ROOT/chroot-exec" /usr/sbin/sshd </dev/null >/dev/null 2>&1 &
    say "sshd starting (enabled earlier)"
  fi
fi

say "up"
