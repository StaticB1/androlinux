# androlinux: grow the rootfs image. Runs on the target as uid 0.
#
# Injected above by container.py:  ALX_IMG ALX_MNT ALX_SIZE
#
# The image is sparse, so its size is a ceiling rather than an allocation — a 120G
# image holding 4G of files still occupies 4G of /data. Growing it therefore costs
# nothing immediately; the number only limits how much of /data the Linux side may
# eventually consume.
#
# Growing only. Shrinking an ext4 filesystem below its used size destroys data, and
# a mistyped number must not be able to do that.
#
# ALL SIZE ARITHMETIC GOES THROUGH awk. Android's shell is 32-bit:
#
#     $ adb shell 'echo $((34359738368))'
#     0
#
# 32 GB is exactly 2^35, which wraps to 0 mod 2^32 — so a byte count in `$(( ))`
# silently becomes nonsense. The first version of this script computed both the
# current and requested sizes that way, got 0 for each, and refused to grow on the
# grounds that 0 is not larger than 0. awk uses doubles and is correct here.

set -eu

say() { printf '  %s\n' "$1"; }

# to_bytes <spec> — "120G", "512M" or a plain byte count, via awk.
to_bytes() {
  awk -v s="$1" 'BEGIN {
    u = substr(s, length(s))
    n = substr(s, 1, length(s) - 1)
    if (u == "G" || u == "g")      printf "%.0f", n * 1073741824
    else if (u == "M" || u == "m") printf "%.0f", n * 1048576
    else if (u == "K" || u == "k") printf "%.0f", n * 1024
    else                           printf "%.0f", s
  }'
}

# gb <bytes> — for display only.
gb() { awk -v b="$1" 'BEGIN { printf "%.1f", b / 1073741824 }'; }

# larger <a> <b> — true when a > b.
larger() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a > b) }'; }

[ -f "$ALX_IMG" ] || { echo "no image at $ALX_IMG" >&2; exit 1; }

# Must be offline: resize2fs will not grow a filesystem it cannot check.
if grep -q " $ALX_MNT " /proc/mounts; then
  echo "the rootfs is mounted — run 'androlinux down' first" >&2
  exit 1
fi

CURRENT=$(stat -c %s "$ALX_IMG")
WANT=$(to_bytes "$ALX_SIZE")

say "current ceiling: $(gb "$CURRENT") GB"
say "requested:       $(gb "$WANT") GB"

if ! larger "$WANT" "$CURRENT"; then
  echo "refusing to shrink: $ALX_SIZE is not larger than the current size." >&2
  echo "Shrinking ext4 below its used size loses data, so this only grows." >&2
  exit 1
fi

# Can /data actually honour the new ceiling? The sparse file will not fail now, but
# promising space that does not exist is worse than saying so.
AVAIL_KB=$(df /data | tail -1 | tr -s ' ' | cut -d' ' -f4)
AVAIL=$(awk -v k="$AVAIL_KB" 'BEGIN { printf "%.0f", k * 1024 }')
GROWTH=$(awk -v w="$WANT" -v c="$CURRENT" 'BEGIN { printf "%.0f", w - c }')
if larger "$GROWTH" "$AVAIL"; then
  say "note: /data has $(gb "$AVAIL") GB free but the ceiling would rise by"
  say "      $(gb "$GROWTH") GB — Linux could be promised more than /data can give."
else
  say "/data has $(gb "$AVAIL") GB free; growth of $(gb "$GROWTH") GB fits"
fi

truncate -s "$ALX_SIZE" "$ALX_IMG"
say "image grown to $ALX_SIZE (sparse — nothing consumed yet)"

# resize2fs insists on a clean filesystem. e2fsck exit 1 means it corrected
# something, which is fine; 2 and above are not.
set +e
e2fsck -f -y "$ALX_IMG" >/dev/null 2>&1
rc=$?
set -e
if [ "$rc" -gt 1 ]; then
  echo "e2fsck failed (exit $rc) — refusing to resize a filesystem it could not check" >&2
  exit 1
fi
say "filesystem checked (e2fsck exit $rc)"

resize2fs "$ALX_IMG" >/dev/null 2>&1
say "filesystem grown to fill the image"

# Report what the filesystem itself now believes, not what we asked for.
BLOCKS=$(dumpe2fs -h "$ALX_IMG" 2>/dev/null | awk -F: '/Block count/  {gsub(/ /,"",$2); print $2}')
BSIZE=$(dumpe2fs  -h "$ALX_IMG" 2>/dev/null | awk -F: '/Block size/   {gsub(/ /,"",$2); print $2}')
if [ -n "${BLOCKS:-}" ] && [ -n "${BSIZE:-}" ]; then
  say "filesystem now $(awk -v b="$BLOCKS" -v s="$BSIZE" 'BEGIN{printf "%.1f", b*s/1073741824}') GB"
fi
say "run 'androlinux up' to mount it again"
