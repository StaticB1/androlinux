#!/usr/bin/env bash
# Build an Android GKI kernel with PID namespaces enabled, bootable by the
# Android emulator, so that systemd can run as PID 1 on the Android kernel.
#
# Why this exists
# ---------------
# systemd refuses to start unless getpid() == 1. Becoming PID 1 without
# replacing Android's init requires a PID namespace, and Google disables that in
# GKI on purpose:
#
#     arch/x86/configs/gki_defconfig:41:  # CONFIG_PID_NS is not set
#
# So no amount of userspace work produces systemd on a stock Android kernel. This
# script rebuilds that kernel with the option turned on.
#
# Usage:  ./build-kernel.sh [-j N]
# Output: kernel/build/arch/x86/boot/bzImage

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/common"
OUT="$HERE/build"
JOBS="${1:-$(nproc)}"
[[ "$JOBS" == -j* ]] && JOBS="${JOBS#-j}"

[[ -d "$SRC" ]] || {
  echo "kernel source missing. Fetch it with:" >&2
  echo "  git clone --depth 1 -b android15-6.6 \\" >&2
  echo "    https://android.googlesource.com/kernel/common $SRC" >&2
  exit 1
}

# Preflight the host toolchain. Every one of these is needed late in the build —
# lz4 only at the very last step, after ~20 minutes of compiling — so checking up
# front is much cheaper than discovering it by failing.
missing=()
for tool in clang ld.lld llvm-ar llvm-nm llvm-objcopy llvm-strip \
            make flex bison bc lz4 zstd cpio pahole rsync; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
[[ -f /usr/include/libelf.h ]] || missing+=("libelf-dev (header)")
[[ -f /usr/include/openssl/ssl.h ]] || missing+=("libssl-dev (header)")
if (( ${#missing[@]} )); then
  echo "missing host build dependencies: ${missing[*]}" >&2
  echo >&2
  echo "  on Debian/Ubuntu:" >&2
  echo "    sudo apt-get install clang lld llvm flex bison bc lz4 zstd cpio \\" >&2
  echo "      dwarves libelf-dev libssl-dev kmod rsync" >&2
  exit 1
fi

MAKE=(make -C "$SRC" ARCH=x86_64 LLVM=1 O="$OUT")

echo "==> base configuration (gki_defconfig)"
# Starting from GKI rather than x86_64_defconfig matters: GKI is what the
# emulator's own kernel is built from, so Android's userspace expectations —
# binder, binderfs, the security model — are already satisfied.
"${MAKE[@]}" gki_defconfig >/dev/null

cfg() { "$SRC/scripts/config" --file "$OUT/.config" "$@"; }

echo "==> enabling namespaces"
# The entire point of the rebuild.
cfg --enable CONFIG_NAMESPACES
cfg --enable CONFIG_PID_NS
# systemd also isolates SysV IPC and expects these to exist.
cfg --enable CONFIG_SYSVIPC
cfg --enable CONFIG_IPC_NS
# Not required for systemd, but it is what lets unprivileged containers and
# rootless podman work inside the distro later.
cfg --enable CONFIG_USER_NS

echo "==> making emulator drivers built-in"
# GKI ships these as modules and expects the vendor ramdisk to supply matching
# ones. A self-built kernel has a different vermagic, so those modules will not
# load — if the disk and console drivers are modules, the kernel boots and then
# panics with no root filesystem. Building them in removes that dependency
# entirely.
for sym in VIRTIO VIRTIO_PCI VIRTIO_PCI_LEGACY VIRTIO_BLK VIRTIO_NET \
           VIRTIO_CONSOLE VIRTIO_MMIO VIRTIO_INPUT VIRTIO_BALLOON \
           VIRTIO_DMA_SHARED_BUFFER NET_9P NET_9P_VIRTIO 9P_FS \
           EXT4_FS EXT4_USE_FOR_EXT2 ANDROID_BINDER_IPC ANDROID_BINDERFS \
           BLK_DEV_LOOP FUSE_FS TUN OVERLAY_FS SCSI_VIRTIO; do
  cfg --enable "CONFIG_$sym"
done

echo "==> making the emulator's platform drivers built-in"
# GKI ships as "generic kernel + vendor modules", and the AVD's vendor partition
# holds modules built against its original kernel. A self-built kernel has a
# different vermagic, so init reports "Exec format error" for every one of them:
#
#   init: Failed to insmod '/vendor/lib/modules/goldfish_pipe.ko': Exec format error
#
# For most of that list the failure is harmless because the driver is built in
# above. goldfish_pipe is not harmless — it is the QEMU pipe transport that adbd
# and the emulator's sensor/battery services talk over. Without it the device
# boots but adb never leaves the "offline" state.
#
# On x86 the GOLDFISH symbol is gated behind X86_GOLDFISH, so that has to come
# first or the rest silently stay unset.
for sym in X86_GOLDFISH GOLDFISH GOLDFISH_PIPE GOLDFISH_TTY \
           BATTERY_GOLDFISH RTC_DRV_GOLDFISH KEYBOARD_GOLDFISH_EVENTS \
           DMABUF_HEAPS DMABUF_HEAPS_SYSTEM RFKILL FAILOVER; do
  cfg --enable "CONFIG_$sym"
done
# Note: goldfish_sync and goldfish_address_space are AOSP out-of-tree drivers and
# are not in kernel/common, so they cannot be built here. They serve gfxstream
# host-GPU passthrough, which is why this kernel is used with a software
# renderer; virtio-gpu below covers /dev/dri for the Linux side.

echo "==> enabling GPU and audio passthrough"
# virtio-gpu is what makes /dev/dri appear in the guest, which is what the Linux
# desktop needs for hardware acceleration. The stock AVD kernel has it unset,
# which is why 'androlinux probe' reported no GPU nodes.
cfg --enable CONFIG_DRM
cfg --enable CONFIG_DRM_VIRTIO_GPU
cfg --enable CONFIG_DRM_FBDEV_EMULATION
cfg --enable CONFIG_SND
cfg --enable CONFIG_SND_VIRTIO

echo "==> relaxing build-hardening that assumes AOSP's exact clang"
# These are correct for Google's build, but they are tied to the specific clang
# revision AOSP pins. Built with a distribution clang they fail or produce a
# kernel that will not load anything. Turning them off costs nothing here: this
# is a development kernel for one emulator, not a shipping image.
cfg --disable CONFIG_CFI_CLANG          # KCFI needs a matching clang
cfg --disable CONFIG_LTO_CLANG
cfg --disable CONFIG_LTO_CLANG_THIN
cfg --disable CONFIG_LTO_CLANG_FULL
cfg --enable  CONFIG_LTO_NONE
cfg --disable CONFIG_DEBUG_INFO_BTF     # pahole version coupling, and slow
cfg --disable CONFIG_MODULE_SIG         # we sign nothing and load no modules
cfg --disable CONFIG_MODULE_SIG_ALL
cfg --disable CONFIG_MODVERSIONS
# CONFIG_SYSTEM_TRUSTED_KEYRING pulls in certs/, whose extract-cert.c does not
# compile against OpenSSL 3 as shipped by Ubuntu 24.04:
#   certs/extract-cert.c:152:7: error: use of undeclared identifier 'key_pass'
# The keyring exists to verify signed modules and IMA policy, neither of which a
# development kernel loading no modules has any use for.
cfg --disable CONFIG_SYSTEM_TRUSTED_KEYRING
cfg --disable CONFIG_SECONDARY_TRUSTED_KEYRING
cfg --disable CONFIG_SYSTEM_REVOCATION_LIST
cfg --set-str CONFIG_SYSTEM_TRUSTED_KEYS ""
cfg --set-str CONFIG_SYSTEM_REVOCATION_KEYS ""
cfg --set-str CONFIG_MODULE_SIG_KEY ""
cfg --disable CONFIG_TRIM_UNUSED_KSYMS
cfg --disable CONFIG_WERROR             # newer clang warns about older code
cfg --disable CONFIG_GKI_HACKS_TO_FIX
cfg --disable CONFIG_RANDSTRUCT_FULL
cfg --disable CONFIG_SHADOW_CALL_STACK

# Let Kconfig settle dependencies rather than trusting the flips above.
"${MAKE[@]}" olddefconfig >/dev/null

echo "==> verifying the configuration actually took"
fail=0
for want in CONFIG_PID_NS CONFIG_IPC_NS CONFIG_NAMESPACES \
            CONFIG_VIRTIO_PCI CONFIG_VIRTIO_BLK CONFIG_VIRTIO_CONSOLE \
            CONFIG_DRM_VIRTIO_GPU CONFIG_GOLDFISH_PIPE CONFIG_DMABUF_HEAPS_SYSTEM; do
  val=$(grep -E "^${want}=" "$OUT/.config" | cut -d= -f2 || true)
  case "$val" in
    y) printf '    %-24s y\n' "$want" ;;
    m) printf '    %-24s m  <-- must be built-in\n' "$want"; fail=1 ;;
    *) printf '    %-24s UNSET  <-- required\n' "$want"; fail=1 ;;
  esac
done
[[ $fail -eq 0 ]] || { echo "configuration is not usable; aborting before the build" >&2; exit 1; }

echo "==> building bzImage with $JOBS jobs (no modules needed)"
# The log is kept and searched on failure. A parallel kernel build prints
# thousands of lines and the real error scrolls past long before the make
# recursion messages that follow it, so "tail" is exactly the wrong tool.
LOG="$HERE/build.log"
if ! time "${MAKE[@]}" -j"$JOBS" bzImage > "$LOG" 2>&1; then
  echo
  echo "build failed. Compiler and linker errors:" >&2
  # "not found" catches a missing host tool (Error 127), which is otherwise
  # invisible among the make recursion messages that follow it.
  grep -nE 'error:|fatal error|undefined symbol|cannot find|No rule to make|not found' "$LOG" \
    | head -20 >&2
  echo >&2
  echo "  full log: $LOG" >&2
  exit 1
fi

IMG="$OUT/arch/x86/boot/bzImage"
[[ -f "$IMG" ]] || { echo "build finished but $IMG is missing" >&2; exit 1; }
echo
echo "  built $IMG ($(du -h "$IMG" | cut -f1))"
echo "  version: $("${MAKE[@]}" -s kernelrelease 2>/dev/null | tail -1)"
echo
echo "  boot it with:"
echo "    emulator -avd androlinux -kernel $IMG -no-window -gpu host"
