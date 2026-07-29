#!/usr/bin/env bash
# Build a kernel that enables PID namespaces *and* still loads the AVD's own
# vendor modules, by matching the stock kernel's identity exactly.
#
# Why not just build GKI from scratch (build-kernel.sh)?
# ------------------------------------------------------
# That works and boots, but Android is shipped as "generic kernel + vendor
# modules" and the AVD's modules are built against its exact kernel. A
# from-scratch kernel has a different vermagic, so init rejects all of them:
#
#   init: Failed to insmod '/vendor/lib/modules/goldfish_pipe.ko': Exec format error
#
# Storage and console can be made built-in, but goldfish_pipe cannot be replaced:
# the in-tree mainline driver's version handshake with the emulator host fails
# (`goldfish_pipe: probe of GFSH0003:00 failed with error -22`), and AOSP's
# working version is out-of-tree. goldfish_pipe is the QEMU pipe transport adbd
# uses, so without it the device boots but adb stays "offline" forever.
#
# The approach here instead:
#   * source at the exact tag the AVD kernel was built from (6.6.50)
#   * the stock kernel's own .config, taken from /proc/config.gz on the device
#   * exactly one option changed: CONFIG_PID_NS=y
#   * CONFIG_LOCALVERSION forced so UTS_RELEASE is byte-identical
#
# vermagic then matches, every vendor module loads, and the only difference from
# the shipped kernel is that systemd can now be PID 1. Signature is not an
# obstacle: the stock config has CONFIG_MODULE_SIG_FORCE unset, so a module whose
# signature does not verify still loads.
#
# Usage:  ./build-kernel-matched.sh <stock.config> [-j N]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STOCK_CFG="${1:?usage: build-kernel-matched.sh <stock.config from /proc/config.gz> [-j N]}"
JOBS="${2:-$(nproc)}"
[[ "$JOBS" == -j* ]] && JOBS="${JOBS#-j}"

SRC="$HERE/common-6.6.50"
OUT="$HERE/build-matched"
TAG="android15-6.6.50_r00"

# The release string the AVD kernel reports, which we must reproduce verbatim.
# Read it off the device with: adb shell uname -r
TARGET_RELEASE="${TARGET_RELEASE:-6.6.50-android15-8-g8adecb593e9b-ab12525588}"

[[ -f "$STOCK_CFG" ]] || { echo "no such config: $STOCK_CFG" >&2; exit 1; }

# ------------------------------------------------------- source at the exact tag
# A separate worktree so the 6.6.142 tree and its build stay intact.
if [[ ! -d "$SRC" ]]; then
  echo "==> creating a worktree at $TAG"
  git -C "$HERE/common" worktree add --detach "$SRC" "refs/tags/$TAG" >/dev/null 2>&1 \
    || git -C "$HERE/common" worktree add --detach "$SRC" FETCH_HEAD
fi
echo "==> source: $(git -C "$SRC" describe --tags --always 2>/dev/null || echo detached)"

# LOCALVERSION= must be *set but empty*. scripts/setlocalversion appends a "+" to
# the release whenever CONFIG_LOCALVERSION_AUTO is off, the tree is a git repo,
# and the LOCALVERSION variable is unset — which would make the release
# "...ab12525588+" and vermagic stop matching. Testing for `${LOCALVERSION+set}`
# is how it decides, so defining it empty is what silences the suffix.
MAKE=(make -C "$SRC" ARCH=x86_64 LLVM=1 O="$OUT" LOCALVERSION=)

mkdir -p "$OUT"
cp "$STOCK_CFG" "$OUT/.config"
echo "==> starting from the stock kernel's own configuration ($(wc -l < "$OUT/.config") lines)"

cfg() { "$SRC/scripts/config" --file "$OUT/.config" "$@"; }

echo "==> the one functional change"
cfg --enable CONFIG_PID_NS
# Deliberately NOT enabling USER_NS or IPC_NS. Both are optional for systemd, and
# every extra flag is another chance to perturb an exported symbol's type and
# break the CONFIG_MODVERSIONS CRCs the vendor modules are checked against.

echo "==> pinning the version string so vermagic matches"
# LOCALVERSION_AUTO would ask git for a revision and produce a different suffix
# than the AVD's build. Pin it literally instead. .scmversion suppresses the "+"
# that setlocalversion otherwise appends to a tree that is not exactly at a tag.
cfg --disable CONFIG_LOCALVERSION_AUTO
cfg --set-str CONFIG_LOCALVERSION "${TARGET_RELEASE#6.6.50}"
: > "$SRC/.scmversion"

echo "==> working around host toolchain differences"
# certs/extract-cert.c does not compile against Ubuntu's OpenSSL 3:
#   certs/extract-cert.c:152:7: error: use of undeclared identifier 'key_pass'
# Dropping the trusted keyring removes certs/ from the build. It affects neither
# vermagic nor the CRCs of the symbols vendor modules import.
cfg --disable CONFIG_SYSTEM_TRUSTED_KEYRING
cfg --disable CONFIG_SECONDARY_TRUSTED_KEYRING
cfg --disable CONFIG_SYSTEM_REVOCATION_LIST
cfg --set-str CONFIG_SYSTEM_TRUSTED_KEYS ""
cfg --set-str CONFIG_SYSTEM_REVOCATION_KEYS ""
# We build no modules here, so nothing needs signing; keeping MODULE_SIG on would
# require generating a signing key we would never use.
cfg --disable CONFIG_MODULE_SIG_ALL
cfg --set-str CONFIG_MODULE_SIG_KEY ""
# AOSP pins a specific clang; these features are tied to it.
cfg --disable CONFIG_CFI_CLANG
cfg --disable CONFIG_LTO_CLANG
cfg --disable CONFIG_LTO_CLANG_THIN
cfg --disable CONFIG_LTO_CLANG_FULL
cfg --enable  CONFIG_LTO_NONE
cfg --disable CONFIG_DEBUG_INFO_BTF
cfg --disable CONFIG_WERROR
# CONFIG_MODVERSIONS stays ON: it is what makes the vendor modules' symbol CRCs
# checkable against ours, and identical source means they should agree.

"${MAKE[@]}" olddefconfig >/dev/null

echo "==> verifying identity before spending a build on it"
release="$("${MAKE[@]}" -s kernelrelease 2>/dev/null | tail -1)"
printf '    kernelrelease  %s\n' "$release"
printf '    target         %s\n' "$TARGET_RELEASE"
if [[ "$release" != "$TARGET_RELEASE" ]]; then
  echo >&2
  echo "    MISMATCH — vendor modules would be rejected, so the build is pointless." >&2
  echo "    Set TARGET_RELEASE to whatever 'adb shell uname -r' prints." >&2
  exit 1
fi
printf '    %-14s %s\n' "PID_NS" "$(grep -E '^CONFIG_PID_NS=' "$OUT/.config" | cut -d= -f2)"
printf '    %-14s %s\n' "MODVERSIONS" "$(grep -E '^CONFIG_MODVERSIONS=' "$OUT/.config" | cut -d= -f2)"
echo "    identity matches — vendor modules should load"

echo "==> building bzImage with $JOBS jobs"
LOG="$HERE/build-matched.log"
if ! time "${MAKE[@]}" -j"$JOBS" bzImage > "$LOG" 2>&1; then
  echo
  echo "build failed:" >&2
  grep -nE 'error:|fatal error|undefined symbol|cannot find|No rule to make|not found' "$LOG" \
    | head -20 >&2
  echo "  full log: $LOG" >&2
  exit 1
fi

IMG="$OUT/arch/x86/boot/bzImage"
[[ -f "$IMG" ]] || { echo "build finished but $IMG is missing" >&2; exit 1; }
echo
echo "  built $IMG ($(du -h "$IMG" | cut -f1))"
echo "  reports itself as: $release"
echo
echo "  boot it with:"
echo "    emulator -avd androlinux -kernel $IMG -no-window -gpu swiftshader_indirect"
