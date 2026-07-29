# androlinux rootfs installer — runs on the target as uid 0.
#
# Variables are injected as shell assignments above this body by rootfs.py:
#   ALX_ROOT ALX_IMG ALX_MNT ALX_TARBALL ALX_SIZE ALX_HOSTNAME ALX_DNS
#
# The rootfs goes into a loopback ext4 image rather than straight onto /data,
# because /data is mounted nosuid,nodev — see config.py for why that matters.

set -eu

say() { printf '  %s\n' "$1"; }

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

mkdir -p "$ALX_ROOT" "$ALX_MNT"

# ---------------------------------------------------------------- ext4 image

if [ ! -f "$ALX_IMG" ]; then
  say "creating $ALX_SIZE sparse ext4 image"
  truncate -s "$ALX_SIZE" "$ALX_IMG"
  # -I 256: the default 128-byte inodes cannot represent dates past 2038.
  # -O ^metadata_csum: Android's e2fsck is older than its mke2fs on some
  #    builds, and a checksum it cannot verify makes the image unrepairable.
  mke2fs -t ext4 -I 256 -O ^metadata_csum -F -q "$ALX_IMG"
  say "formatted ext4"
else
  say "reusing existing image $ALX_IMG"
fi

# -------------------------------------------------------------------- mount

if grep -q " $ALX_MNT " /proc/mounts; then
  say "already mounted"
else
  mount -o loop,rw,suid,dev,exec "$ALX_IMG" "$ALX_MNT"
  say "mounted with suid,dev,exec"
fi

# Confirm the flags actually took: a silently nosuid root would break sudo in
# ways that surface much later and look like a distro bug.
OPTS=$(grep " $ALX_MNT " /proc/mounts | head -1 | cut -d' ' -f4)
case ",$OPTS," in
  *,nosuid,*) echo "FATAL: rootfs mounted nosuid ($OPTS)" >&2; exit 1 ;;
  *,nodev,*)  echo "FATAL: rootfs mounted nodev ($OPTS)"  >&2; exit 1 ;;
esac
say "mount options verified: $OPTS"

# ------------------------------------------------------------------ unpack

if [ -x "$ALX_MNT/bin/sh" ] || [ -L "$ALX_MNT/bin/sh" ]; then
  say "rootfs already unpacked, skipping extraction"
else
  say "unpacking $(basename "$ALX_TARBALL") ..."
  # --numeric-owner: the tarball's uid/gid numbers are authoritative; we must
  # not remap them through Android's /etc/passwd, which has no distro users.
  tar -xzf "$ALX_TARBALL" -C "$ALX_MNT" --numeric-owner
  say "unpacked"
fi

# ------------------------------------------------------------------ configure
# File-level setup only. Anything needing to run *inside* the distro happens in
# a later step, once the chroot is proven to work.

# Distro images ship several /etc files as symlinks into runtime directories that
# do not exist yet — Debian points /etc/resolv.conf at
# /run/systemd/resolve/stub-resolv.conf. Redirecting into a dangling symlink
# fails with ENOENT, so replace the link rather than following it.
unlink_first() { [ -L "$1" ] && rm -f "$1"; return 0; }

unlink_first "$ALX_MNT/etc/hostname"
printf '%s\n' "$ALX_HOSTNAME" > "$ALX_MNT/etc/hostname"

unlink_first "$ALX_MNT/etc/resolv.conf"
: > "$ALX_MNT/etc/resolv.conf"
for ns in $ALX_DNS; do
  printf 'nameserver %s\n' "$ns" >> "$ALX_MNT/etc/resolv.conf"
done

grep -q androlinux "$ALX_MNT/etc/hosts" 2>/dev/null || cat >> "$ALX_MNT/etc/hosts" <<EOF
127.0.0.1	localhost $ALX_HOSTNAME
::1	localhost ip6-localhost ip6-loopback
EOF

# There is no init to talk to, so package postinst scripts must not try to
# start services. Without this, installing anything that ships a unit or an
# init script fails the whole apt transaction.
mkdir -p "$ALX_MNT/usr/sbin"
cat > "$ALX_MNT/usr/sbin/policy-rc.d" <<'EOF'
#!/bin/sh
# androlinux: no init is running in this rootfs, so decline service actions.
exit 101
EOF
chmod 755 "$ALX_MNT/usr/sbin/policy-rc.d"

# The linuxcontainers images ship a minimal sources.list; make sure updates and
# security are present so a desktop can actually be installed.
if [ -f "$ALX_MNT/etc/apt/sources.list" ] || [ -d "$ALX_MNT/etc/apt/sources.list.d" ]; then
  mkdir -p "$ALX_MNT/etc/apt/apt.conf.d"
  # Recommends off by default: with no init running, recommended packages that
  # ship daemons cannot be started, and they roughly triple the install size.
  # Individual installs can still opt in with --install-recommends.
  #
  # APT::Sandbox::User "root" is not laziness. apt normally drops to the unpriv
  # "_apt" user to fetch packages, but Android gates socket creation on group
  # membership (AID_INET, 3003) and a chroot process has no supplementary groups,
  # so _apt cannot open a socket at all. apt then reports the misleading
  #   Temporary failure resolving 'deb.debian.org'
  # even though resolution works perfectly as root. Verified on an SM-T970:
  # `getent hosts` succeeds as root and fails as _apt on the same rootfs.
  cat > "$ALX_MNT/etc/apt/apt.conf.d/90androlinux" <<'APTEOF'
APT::Install-Recommends "false";
APT::Sandbox::User "root";
APTEOF
fi

mkdir -p "$ALX_MNT/proc" "$ALX_MNT/sys" "$ALX_MNT/dev/pts" "$ALX_MNT/dev/shm" \
         "$ALX_MNT/run" "$ALX_MNT/tmp"
chmod 1777 "$ALX_MNT/tmp"

say "configured hostname=$ALX_HOSTNAME dns=$ALX_DNS"
say "installed at $ALX_MNT"
