# androlinux: tear the rootfs down. Runs on the target as uid 0.
#
# Injected above by container.py:  ALX_MNT  ALX_ROOT
#
# Order matters. Unmounting must run deepest-first, or a busy parent refuses to
# detach and leaves the loop device attached — after which the next `up` mounts a
# second copy of the same image. /proc/mounts is sorted by path length descending
# to get that ordering without hardcoding the list.

set -eu

say() { printf '  %s\n' "$1"; }

# Anything still running inside the rootfs holds mounts busy.
for pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
  root=$(readlink "/proc/$pid/root" 2>/dev/null || true)
  case "$root" in
    "$ALX_MNT"*) say "killing pid $pid ($(cat "/proc/$pid/comm" 2>/dev/null || echo ?))"
                 kill -9 "$pid" 2>/dev/null || true ;;
  esac
done

targets=$(awk -v m="$ALX_MNT" '$2 == m || index($2, m "/") == 1 { print length($2), $2 }' \
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

# mount -o loop attaches a loop device implicitly; umount usually detaches it,
# but a lazy unmount does not. Reclaim any left pointing at our image.
for ld in $(ls /dev/block/loop* 2>/dev/null); do
  back=$(losetup "$ld" 2>/dev/null | grep -o '/data/androlinux/[^ ]*' || true)
  if [ -n "$back" ]; then
    losetup -d "$ld" 2>/dev/null && say "detached $ld"
  fi
done

say "down"
