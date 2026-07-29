# androlinux: remove an instance, or everything. Runs on the target as uid 0.
#
# Injected above by container.py:  ALX_BASE ALX_TARGET ALX_ALL
#
# Deleting the image file is the easy part. The order matters, because a mounted
# image whose file is unlinked leaves the loop device attached and the space
# unreclaimed — `df` then disagrees with `du` until reboot. So: kill what is
# inside, unmount deepest-first, detach the loop devices, and only then delete.

set -u

say() { printf '  %s\n' "$1"; }

# Refuse to operate outside our own tree, whatever we were handed.
case "$ALX_TARGET" in
  /data/androlinux|/data/androlinux/*) ;;
  *) echo "refusing to delete $ALX_TARGET — outside $ALX_BASE" >&2; exit 1 ;;
esac

if [ ! -e "$ALX_TARGET" ]; then
  say "nothing at $ALX_TARGET"
  exit 0
fi

BEFORE=$(df /data 2>/dev/null | tail -1 | tr -s ' ' | cut -d' ' -f4)

# 1. Anything running inside a rootfs holds its mounts busy.
for pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
  root=$(readlink "/proc/$pid/root" 2>/dev/null || true)
  case "$root" in
    "$ALX_TARGET"*)
      say "killing pid $pid ($(cat "/proc/$pid/comm" 2>/dev/null || echo ?))"
      kill -9 "$pid" 2>/dev/null || true
      ;;
  esac
done

# 2. Deepest-first, or a busy parent refuses to detach.
targets=$(awk -v t="$ALX_TARGET" '$2 == t || index($2, t "/") == 1 { print length($2), $2 }' \
          /proc/mounts | sort -rn | cut -d' ' -f2-)
for mp in $targets; do
  if umount "$mp" 2>/dev/null; then
    say "unmounted $mp"
  elif umount -l "$mp" 2>/dev/null; then
    say "lazily unmounted $mp (was busy)"
  else
    say "FAILED to unmount $mp"
  fi
done

# 3. Detach loop devices still backed by files we are about to delete. Skipping
#    this is what leaks the space.
for ld in $(ls /dev/block/loop* 2>/dev/null); do
  back=$(losetup "$ld" 2>/dev/null | grep -oE "$ALX_TARGET/[^ ]*" || true)
  if [ -n "$back" ]; then
    losetup -d "$ld" 2>/dev/null && say "detached $ld ($back)"
  fi
done

# 4. Delete.
if [ "${ALX_ALL:-0}" = "1" ]; then
  rm -rf "$ALX_TARGET"
  say "removed $ALX_TARGET"
else
  # A named instance is a self-contained directory. The unnamed one shares
  # /data/androlinux with any named instances, so only its own files may go —
  # never the whole directory.
  if [ "$ALX_TARGET" = "$ALX_BASE" ]; then
    for f in rootfs.img mnt stage chroot-exec boot.sh up.sh start display \
             default-user boot.log systemd.pid systemd.log; do
      rm -rf "$ALX_TARGET/$f" 2>/dev/null || true
    done
    say "removed the unnamed instance's files, leaving any named instances alone"
  else
    rm -rf "$ALX_TARGET"
    say "removed $ALX_TARGET"
  fi
fi

AFTER=$(df /data 2>/dev/null | tail -1 | tr -s ' ' | cut -d' ' -f4)
if [ -n "${BEFORE:-}" ] && [ -n "${AFTER:-}" ]; then
  FREED=$(( (AFTER - BEFORE) / 1024 ))
  say "reclaimed ${FREED} MB on /data"
fi

say "remaining under $ALX_BASE: $(ls -A "$ALX_BASE" 2>/dev/null | tr '\n' ' ' || echo '(gone)')"
