# androlinux target probe — emits `key=value` lines on stdout.
#
# POSIX sh only. Android ships mksh with toybox applets: no arrays, no [[ ]], no
# `local`, no process substitution, and a `grep` without GNU extensions.
#
# The probe answers the questions that decide whether a native Linux userspace is
# possible at all. Three can kill the design outright, so they are tested by
# *doing* rather than by inference:
#
#   1. Is /data mounted noexec or nosuid? A rootfs there could not run binaries.
#   2. Can we create a PID namespace? Without one, systemd cannot be PID 1.
#   3. Can we mount? Without mount there is no /proc, /sys or /dev in the rootfs.
#
# Reported capabilities are prefixed so the host side can group them:
#   android.* os identity   kernel.* kernel identity   bin.*  tooling present
#   kcfg.*    kernel config ns.*     namespace support test.* live experiments
#   dev.*     device nodes   mount.*  mount state       cgroup.* cgroup layout

p() { printf '%s=%s\n' "$1" "$2"; }

have() {
  _found=""
  for _d in $(echo "$PATH" | tr ':' ' '); do
    if [ -x "$_d/$1" ]; then _found="$_d/$1"; break; fi
  done
  p "bin.$1" "$_found"
}

# ------------------------------------------------------------------- identity

p android.release "$(getprop ro.build.version.release 2>/dev/null)"
p android.sdk     "$(getprop ro.build.version.sdk 2>/dev/null)"
p android.build   "$(getprop ro.build.type 2>/dev/null)"
p android.device  "$(getprop ro.product.model 2>/dev/null)"
p android.abi     "$(getprop ro.product.cpu.abi 2>/dev/null)"
p kernel.release  "$(uname -r 2>/dev/null)"
p kernel.arch     "$(uname -m 2>/dev/null)"
p id.uid          "$(id -u 2>/dev/null)"

# SELinux in enforcing mode will deny most of what a foreign rootfs does.
p selinux.mode "$(getenforce 2>/dev/null)"
[ -d /sys/fs/selinux ] && p selinux.fs yes || p selinux.fs no

# --------------------------------------------------------------- /data state
# The rootfs lives on /data. Its mount flags govern whether it can host one.

_dline=$(grep ' /data ' /proc/mounts 2>/dev/null | head -1)
p mount.data.fstype "$(echo "$_dline" | cut -d' ' -f3)"
p mount.data.opts   "$(echo "$_dline" | cut -d' ' -f4)"
case ",$(echo "$_dline" | cut -d' ' -f4)," in
  *,noexec,*) p mount.data.noexec yes ;;
  *)          p mount.data.noexec no  ;;
esac
case ",$(echo "$_dline" | cut -d' ' -f4)," in
  *,nosuid,*) p mount.data.nosuid yes ;;
  *)          p mount.data.nosuid no  ;;
esac
case ",$(echo "$_dline" | cut -d' ' -f4)," in
  *,nodev,*) p mount.data.nodev yes ;;
  *)         p mount.data.nodev no  ;;
esac
p data.free_kb "$(df /data 2>/dev/null | tail -1 | tr -s ' ' | cut -d' ' -f4)"

# ------------------------------------------------------------------- tooling
# We need a namespace-capable launcher and an unpacker. Android has neither
# reliably, so record exactly what exists before deciding what to ship.

for _b in unshare nsenter chroot mount umount losetup mknod tar gzip xz \
          busybox toybox setenforce start-stop-daemon dd truncate mke2fs; do
  have "$_b"
done

# ------------------------------------------------------------- kernel config
# Present on most Android kernels as a gzipped blob. When absent we fall back
# entirely on the live tests below.

KCFG=""
if [ -r /proc/config.gz ] && command -v zcat >/dev/null 2>&1; then
  KCFG=$(zcat /proc/config.gz 2>/dev/null)
elif [ -r /proc/config.gz ] && command -v gzip >/dev/null 2>&1; then
  KCFG=$(gzip -dc /proc/config.gz 2>/dev/null)
fi
if [ -n "$KCFG" ]; then
  p kcfg.source /proc/config.gz
  for _k in CONFIG_NAMESPACES CONFIG_PID_NS CONFIG_MOUNT_NS CONFIG_USER_NS \
            CONFIG_NET_NS CONFIG_UTS_NS CONFIG_IPC_NS CONFIG_CGROUPS \
            CONFIG_OVERLAY_FS CONFIG_EXT4_FS CONFIG_BLK_DEV_LOOP \
            CONFIG_FUSE_FS CONFIG_SECCOMP CONFIG_TUN CONFIG_VETH \
            CONFIG_BRIDGE CONFIG_DRM CONFIG_DRM_VIRTIO_GPU; do
    _v=$(echo "$KCFG" | grep "^${_k}=" | head -1 | cut -d= -f2)
    p "kcfg.$_k" "${_v:-unset}"
  done
else
  p kcfg.source unavailable
fi

# ---------------------------------------------------------------- namespaces

for _n in pid mnt net uts ipc user cgroup time; do
  [ -e "/proc/self/ns/$_n" ] && p "ns.$_n" yes || p "ns.$_n" no
done

# -------------------------------------------------------------------- cgroups
# systemd wants a writable cgroup hierarchy and strongly prefers v2 (unified).

if [ -e /sys/fs/cgroup/cgroup.controllers ]; then
  p cgroup.version v2
  p cgroup.controllers "$(cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null | tr ' ' ',')"
elif [ -d /sys/fs/cgroup ]; then
  p cgroup.version v1
  p cgroup.controllers "$(ls /sys/fs/cgroup 2>/dev/null | tr '\n' ',')"
else
  p cgroup.version none
fi

# --------------------------------------------------------------- device nodes
# What the Linux side could reach for hardware: GPU, sound, tun, loop, binder.

for _d in /dev/dri/card0 /dev/dri/renderD128 /dev/snd /dev/fuse /dev/tun \
          /dev/loop-control /dev/binder /dev/ashmem /dev/kvm /dev/input; do
  [ -e "$_d" ] && p "dev.$_d" yes || p "dev.$_d" no
done
p dev.loop_count "$(ls /dev/block/loop* 2>/dev/null | wc -l | tr -d ' ')"

# ------------------------------------------------------------- live tests
# Inference is not enough for the load-bearing capabilities. Test them.

T=/data/local/tmp/.alx-probe
rm -rf "$T" 2>/dev/null
mkdir -p "$T" 2>/dev/null

# Can a binary on /data actually execute? Fatal if not.
printf '#!/system/bin/sh\necho alx-exec-ok\n' > "$T/t.sh" 2>/dev/null
chmod 755 "$T/t.sh" 2>/dev/null
if [ "$("$T/t.sh" 2>/dev/null)" = "alx-exec-ok" ]; then
  p test.data_exec pass
else
  p test.data_exec fail
fi

# Everything below needs uid 0; skip cleanly rather than reporting false negatives.
if [ "$(id -u)" != "0" ]; then
  p test.privileged skipped-not-root
else
  p test.privileged yes

  if mkdir -p "$T/mnt" 2>/dev/null && mount -t tmpfs tmpfs "$T/mnt" 2>/dev/null; then
    p test.mount_tmpfs pass
    umount "$T/mnt" 2>/dev/null
  else
    p test.mount_tmpfs fail
  fi

  if mknod "$T/nullnode" c 1 3 2>/dev/null; then
    p test.mknod pass
    rm -f "$T/nullnode" 2>/dev/null
  else
    p test.mknod fail
  fi

  # The decisive one: a PID namespace where our child sees itself as PID 1.
  # Wrapped in a timeout: on some kernels unshare blocks rather than failing,
  # and a probe must never be the thing that hangs.
  if command -v unshare >/dev/null 2>&1; then
    if command -v timeout >/dev/null 2>&1; then
      _r=$(timeout 10 unshare --pid --fork --mount-proc sh -c 'echo pid=$$' 2>/dev/null)
    else
      _r=$(unshare --pid --fork --mount-proc sh -c 'echo pid=$$' 2>/dev/null)
    fi
    case "$_r" in
      pid=1) p test.pid_ns pass ;;
      *)     p test.pid_ns "fail:${_r:-no-output}" ;;
    esac
  else
    p test.pid_ns no-unshare
  fi

  if losetup -f >/dev/null 2>&1; then
    p test.loop_free "$(losetup -f 2>/dev/null)"
  else
    p test.loop_free none
  fi
fi

rm -rf "$T" 2>/dev/null
p probe.done 1
