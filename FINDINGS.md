# androlinux: engineering log

Everything that went wrong building this, why, and what fixed it. Written because
most of these failures were *confidently wrong* rather than loud — a check that
passed when it should not have, a command that reported success having done
nothing — and that class of bug is worth a permanent record.

Each entry gives the symptom, the root cause, the evidence, and the fix. Where a
bug was mine rather than an environment quirk, it says so.

---

## What was built

A tool that runs a full Linux distribution on an Android device's own kernel — no
virtual machine, no syscall translation. The distro's userspace executes directly
on the running kernel, isolated by a chroot and mount namespace.

Verified on two targets:

| | Emulator | Tablet |
|---|---|---|
| Device | AVD `androlinux` | Samsung Galaxy Tab S7+ (SM-T970) |
| Android | 15 / API 35, userdebug | 13 / API 33, **user** build |
| Kernel | 6.6.50-android15 | 4.19.113 (Snapdragon 865+) |
| Arch | x86_64 | aarch64 |
| Root via | `adb root` | Magisk `su` |
| `/data` | ext4, `nosuid,nodev` | **f2fs**, `nosuid,nodev` |
| Guest | Debian 12 amd64 | Debian 12 + **Ubuntu 24.04.4** arm64 |

End state on the tablet: Ubuntu 24.04.4 LTS arm64 matching the host workstation
release-for-release, XFCE at 2560×1600 shown on the tablet's own screen, running as
a normal user, surviving reboots unattended, startable from the device with no host
attached.

---

## Three decisions the findings forced

**The rootfs lives in a loopback ext4 image, not on `/data`.** `/data` is mounted
`nosuid,nodev` on both targets. Unpacked directly there, every setuid binary
(`sudo`, `ping`) silently loses its privilege bit and device nodes under the
rootfs's own `/dev` do not function. An image file gets its own mount options, so
it can be `suid,dev,exec`. `install` verifies the flags actually took rather than
assuming.

**`/dev` is built, not bind-mounted from Android.** Bind-mounting Android's `/dev`
would leak every `mkdir` the distro does into Android's live `/dev` and hand the
rootfs every node on the device. A fresh tmpfs with explicitly created nodes makes
hardware exposure a decision rather than an accident.

**Shell work is pushed as a file and run by path.** Forced by three measured adb
behaviours — see A1–A3.

---

## A. The adb transport

### A1. `adb exec-out` silently discards exit status
**Symptom.** Failing scripts reported success.
**Evidence.**
```
$ adb exec-out sh -c 'exit 7'; echo $?      → 0
$ adb shell 'exit 7'; echo $?               → 7
```
**Fix.** `adb shell` with a command argument is the execution path. Contrary to the
usual warning about pty translation, it emits LF-only output when non-interactive —
verified with `cat -A`.

### A2. `adb exec-out sh` ignores piped stdin and hangs
**Symptom.** The first probe run hung until killed.
**Cause.** It opens an interactive shell and waits on a terminal that never comes.
**Fix.** Never feed scripts over stdin.

### A3. adb flattens argv and the remote shell re-splits it
**Cause.** Quotes, newlines and `$` in a large script do not survive. A rootfs
bootstrap is hundreds of lines of nested quoting.
**Fix.** Push the script as a file, run `adb shell sh <path>`. Removes every
quoting layer between host and device shell, and leaves the exact script on the
device for inspection.

### A4. `su -c` received only the first word of its command *(my bug)*
**Cause.** Passing `("su", "-c", "sh /path")` is flattened by adb to `su -c sh
/path`; the remote shell re-splits, so `su` sees `-c sh` and `/path` as a separate
argument.
**Fix.** Send one pre-quoted string: `su -c 'sh /path'`.

### A5. `adb push` cannot write to `/data/androlinux` under `su` *(my bug)*
**Symptom.**
```
adb: error: failed to copy '...': remote couldn't create file: Permission denied
...then, confusingly: "1 file pushed, 0 skipped. 34.0 MB/s"
```
**Cause.** Pushes are performed by adbd, which on a user build runs as the *shell*
user. Obtaining root through `su` does not change that. The emulator hid this
entirely because adbd was already root there.
**Fix.** `Adb.push_root` stages into `/data/local/tmp` (shell-writable) and moves
the file into place as root — a rename, since both are on `/data`, so it costs
nothing on a 163 MB file.

### A6. A push can truncate without reporting failure
**Evidence.** A 163 MB tarball landed as 80,609,277 bytes after the tablet dropped
off the bus mid-transfer.
**Fix.** `push_root` compares the transferred size against the local file.

### A7. adb exits 0 when the device vanishes mid-script *(my bug)*
**Symptom.** `install` printed `done —` with none of its progress lines. Nothing
had been created.
**Cause.** The tablet rebooted part-way; adb returned the output it had already
received and exited 0. The code trusted the status.
**Fix.** `install` verifies the postcondition on the device — image present, rootfs
unpacked — instead of trusting the exit code, and says plainly that a disconnect
can look like success.

### A8. `adb pull` runs unprivileged *(my bug, latent)*
**Cause.** Same root as A5, in the other direction: `gui screenshot` could not read
its own PNG out of `/data/androlinux` on a Magisk device.
**Fix.** Stage to `/data/local/tmp` as root, make it readable, pull, clean up.

---

## B. Filesystem and rootfs

### B1. `/etc/resolv.conf` is a dangling symlink in a fresh rootfs
**Symptom.** `install` died with
`can't create /data/androlinux/mnt/etc/resolv.conf: No such file or directory`,
*after* successfully unpacking 435 MB.
**Cause.** Debian ships `/etc/resolv.conf → /run/systemd/resolve/stub-resolv.conf`.
Redirecting into a dangling symlink fails with ENOENT.
**Fix.** Unlink before writing. Applies to any `/etc` file a distro points into a
runtime directory.

### B2. Android has no `xz`
**Cause.** The image server publishes `rootfs.tar.xz`; the probe confirmed `gzip`
and `tar` present, `xz` absent.
**Fix.** Recompress to gzip on the host. Costs ~60 MB more transfer, cheaper than
shipping an `xz` binary per architecture.

### B3. `mke2fs` defaults are wrong for this use
**Cause.** 128-byte inodes cannot represent dates past 2038 (`mke2fs` says so
itself), and Android's `e2fsck` is older than its `mke2fs` on some builds, so a
checksum it cannot verify makes the image unrepairable.
**Fix.** `-I 256 -O ^metadata_csum`.

### B4. toybox `mount --bind` loop-mounts device *files*
**Symptom.** `/dev/fuse` and `/dev/tun` failed to expose:
`losetup: /dev/block/loop44=/dev/fuse: Invalid argument`.
**Cause.** Android's `mount` sees a non-directory source and tries to attach it as
a loop device.
**Fix.** Recreate character/block devices with `mknod` at the same major/minor —
which is also what a real `/dev` contains. Directories (`/dev/snd`, `/dev/dri`)
still need a bind, and that does work. Note toybox `stat` reports major/minor in
hex while `mknod` wants decimal.

---

## C. SELinux, and three reboots of somebody's tablet

### C1. `setenforce 0` panics a Samsung kernel *(my bug — the worst one)*
**Symptom.** The Tab S7+ rebooted every time `install` or `up` ran. `probe` was
always fine. It happened at 19% battery and again at 70%, so it was not power.
**Evidence.** A log synced either side of the call:
```
pre=Enforcing        ← written and flushed
                     ← "rc=" and "post=" never appear
```
The kernel died *inside* that one call. Host `dmesg` confirmed USB re-enumeration
rather than a cable fault. Knox properties present throughout.
**Cause.** Samsung's Knox/RKP treats disabling SELinux enforcement as tampering and
reboots on the spot. I was issuing `setenforce 0` unconditionally at the top of
both `install` and `up`.
**Fix.** androlinux never touches SELinux unless `--permissive` is passed, and the
flag's help text names the hazard.
**And it was unnecessary.** Magisk's `su` runs in an unconfined domain, so mounting
with `suid,dev` and `mknod` both succeed while enforcing:
```
SELinux left as-is (Enforcing)
mount options verified: rw,seclabel,relatime,i_version
```
The whole tablet setup now runs with SELinux enforcing.

---

## D. Networking

### D1. The emulator's resolver was hardcoded first *(my bug)*
**Symptom.** On the tablet, `getent hosts deb.debian.org` resolved but
`apt-get update` reported `Temporary failure resolving`.
**Cause.** `10.0.2.3` is the emulator's gateway DNS and is unroutable on real
hardware. Listed first, every lookup waits for it to time out; glibc eventually
falls through — so `getent` appears fine — while apt gives up first.
**Fix.** Dropped. `resolv.conf` is rebuilt on every `up` from Android's own
`net.dns*` properties, falling back to public resolvers. Rebuilding each time also
handles a device changing networks.

### D2. apt's sandbox user has no network at all
**Symptom.** `Temporary failure resolving` persisted with a clean `resolv.conf`.
**Cause.** Android gates socket *creation* on group membership: a process must be in
`AID_INET` (gid 3003). A chroot process has no supplementary groups. apt drops to
the unprivileged `_apt` user to fetch packages, which then cannot open a socket —
and reports it as DNS.
**Evidence.**
```
getent hosts deb.debian.org                                    → resolves
setpriv --reuid=_apt --clear-groups getent hosts deb.debian.org → fails
```
**Fix.** `install` writes `APT::Sandbox::User "root";`.

### D3. A normal user has no network either
Same cause as D2, and the reason `user add` is a command rather than a documented
`useradd`:
```
setpriv --reuid=1000 --regid=1000 --clear-groups getent hosts deb.debian.org → FAILS
setpriv --reuid=1000 --regid=1000 --groups=3003  getent hosts deb.debian.org → RESOLVES
```
**Fix.** The user joins groups whose gids match Android's AIDs — 3003 inet, 3004
net_raw so `ping` works, 3005 net_admin, 1015/1023 storage, 9997 everybody — and
`user add` *proves* the account can resolve before reporting success.

---

## E. The desktop

### E1. A daemon holds adb's process group open forever *(my bug)*
**Symptom.** `gui start` hung for the full 600 s timeout, while the session had in
fact started correctly — port 5901 was listening.
**Cause.** `adb shell` waits for the whole process group. A surviving X server keeps
the call alive.
**Fix.** `setsid` plus `</dev/null` and a log redirect, releasing every inherited
descriptor. Load-bearing, not decoration — the same pattern is needed for
websockify and for the boot hook's desktop start.

### E2. TigerVNC's binary is `Xtigervnc`, not `Xvnc`
A `pgrep Xvnc` check silently missed a running server.

### E3. `-localhost no` was both unnecessary and unsafe *(my bug)*
**Symptom.** TigerVNC refused outright:
`YOU ARE TRYING TO EXPOSE A VNC SERVER WITHOUT ANY AUTHENTICATION...`
**Cause.** I bound `0.0.0.0` thinking `adb forward` needed it. It does not — adb's
forwarder runs *on the device* and connects to `127.0.0.1`.
**Fix.** Loopback only. The desktop is never exposed on the device's Wi-Fi, and
TigerVNC stops objecting. The same mistake was repeated once with websockify and
fixed the same way.

### E4. `gui screenshot` reported success on a 0-byte PNG *(my bug)*
**Cause.** Three things compounding: `container.run` sets `pipefail` but not
`set -e`; `> /tmp/shot.png` creates the file *before* `pnmtopng` runs; and `ls` as
the last command exits 0. So `xwd` failing was completely silent, and the caller
printed `wrote shot.png`.
**Fix.** `set -e` plus an explicit `test -s`. Verified: `--display :9` now exits 2
with `xwd: unable to open display ':9'` and writes no file.

### E5. `gui install` could never report failure *(my bug)*
Same missing `set -e`; the trailing `echo` always succeeded, so a failed `apt-get`
printed a cheerful `installed: N packages`. The `raise` below it was dead code.

### E6. `gui start` could strand a live X server *(my bug)*
**Cause.** It removed the X socket even when `vncserver -kill` had missed — and
`-kill` does go stale, since it locates its target through a pidfile. A live server
then keeps port 5901, the replacement cannot bind, and the caller waits out a 45 s
poll before failing, with the original session now unreachable over its socket too.
**Fix.** Wait for the process to actually die; refuse to unlink otherwise.

### E7. Tapping the home-screen widget destroyed the running desktop *(my bug)*
**Symptom.** User report: "tapping the widget when it's already running is crashing
it."
**Cause.** `start` always ran the desktop-start step, which treated an existing X
socket as *stale* and killed the server to replace it. Every open window died and
the connected viewer dropped — indistinguishable from a crash. I had also
documented it as "safe to run when things are already up". It was not.
**Fix.** Two levels. `start` short-circuits to raising the viewer when the rootfs
is mounted and a server is live; `gui-start.sh` refuses to replace a running server
without `ALX_FORCE=1`. An explicit `--restart` remains for when you do want a fresh
session.
**Verified.**
```
Xtigervnc PID before: 20098
  ... tap again ...
Xtigervnc PID after:  20098      ← untouched
```
plus an open terminal surviving two taps.

### E8. The viewer launched the wrong activity — "missing server info" *(my bug)*
**Cause.** I read `com.gaurav.avnc/.ui.vnc.VncActivity` off `dumpsys` *after* a
successful launch and assumed it was the entry point. The activity that *handles*
the `vnc://` URI is `UriReceiverActivity`; naming `VncActivity` directly starts it
with no host or port. My original manual command had no `-n` at all, which is why
it worked.
**Fix.** Resolve the handler at runtime:
```
adb shell "cmd package resolve-activity -a android.intent.action.VIEW -d 'vnc://127.0.0.1:5901'"
  → com.gaurav.avnc/com.gaurav.avnc.UriReceiverActivity
```
Which also keeps it working with any other `vnc://` handler.

### E9. noVNC packaging is not what the names suggest
`/usr/bin/websockify` comes from **python3-websockify**; the `websockify` package
ships only `/usr/bin/rebind`. There is no `/usr/bin/novnc_proxy` — it lives at
`/usr/share/novnc/utils/novnc_proxy`.

### E10. `localhost` is the wrong host for websockify
websockify binds IPv4 only, while Android resolves `localhost` to `::1` as well, so
the name can cost a refused connection before the browser falls back. The URL uses
`127.0.0.1` literally.

---

## F. Running two distros at once

Adding Ubuntu alongside Debian surfaced a cluster of related bugs.

### F1. Instances cannot share an X display
**Cause.** They share one network namespace, so the second server finds the RFB
port taken:
`A VNC server is already running as :1 on machine localhost`.
**Fix.** Each instance is allocated a display the first time it starts one and
remembers it. `viewer`, `web`, `screenshot` and `stop` all follow the recorded
value.

### F2. `pgrep` cannot see other users' processes
**Symptom.** With the desktop now running as a normal user, a root-side "is a
session already running?" check reported nothing — and happily started a second
server on a port already in use.
**Evidence.**
```
proc /proc proc rw,relatime,gid=3009,hidepid=2
pgrep as b:                          ← empty
port 5901 reachable from b: YES
```
**Cause.** Android mounts `/proc` with `hidepid=2`.
**Fix.** Liveness is decided by connecting to the RFB port — uid-independent. This
also weakened the E7 fix until corrected, since that check was pgrep-based.

### F3. Device scripts run in mksh, so bash's `/dev/tcp` silently fails *(my bug)*
**Symptom.** "First free display" returned `:1` every time, including when `:1` was
plainly taken.
**Cause.** I wrote the port probe with bash's `/dev/tcp`, but that script runs in
Android's shell, which has no such feature — so every probe "failed" and the first
candidate always won. Port questions are now asked of `ss`.

### F4. A remembered display was trusted without validation *(my bug)*
The buggy F3 probe *recorded* `:1`, and every later run then short-circuited onto
that broken choice. A stored display is now re-validated: good only if the port is
free, or busy because of this instance's own server.

---

## G. Tool correctness

### G1. `-s` after the subcommand was a hard failure *(my bug)*
`--serial` existed only on the top-level parser, so the natural `alx up -s <serial>`
died with `unrecognized arguments`. Fixed with a shared parent parser — and
`default=argparse.SUPPRESS` is load-bearing, or the subparser writes its own `None`
over a value given earlier.

### G2. A fixed staging path raced *(my bug)*
`container.run` staged every command to one constant path in two separate adb round
trips. A concurrent run could overwrite the payload in between, and this process
would then execute someone else's command and report it as its own. Caught in the
act during a parallel review, with another agent's script sitting in the file.
Fixed with uuid-named staging.

### G3. Staging could execute a stale file *(my bug)*
No `set -e` in the staging script, and `chmod` last — so a failed `cat` (a full
image) still looked successful and left the previous invocation's command to run.

### G4. `run` flattened quoting *(my bug)*
`run python3 -c "print('a b')"` became `print(a b)` → SyntaxError. Now a single
argument passes through untouched and multiple arguments are re-quoted.

### G5. The probe promised a strategy that did not exist *(my bug)*
`proot` was named in the verdict and never implemented, and `Verdict.viable`
ignored blockers — so an unrooted device was told it was fine and then failed at
the first mount. `proot` is now reported as a blocker.

### G6. `autostart` and `up` drifted, and only a reboot could show it *(my bug)*
**Symptom.** After a real reboot: the hook mounted the rootfs and then died with
`up.sh: line 71: ALX_DNS: parameter not set`, leaving a half-up system.
**Cause.** The boot hook stages its *own standalone copy* of `up.sh` so a reboot
needs no host. I added `ALX_DNS` for `up` and missed the hook. `set -u` did the
rest.
**Fix.** Both paths render through one `container.up_script()`, so they cannot drift
again, plus a `${ALX_DNS:-...}` backstop — a boot hook failing silently is the worst
place for a missing variable.

### G7. Smaller ones
- `autostart` hardcoded the desktop geometry and display `:1`, wrong for any other
  instance. Both are now parameters.
- `install.sh` wrote `APT::Install-Recommends "true"` directly under a comment
  explaining why services must not start.
- `down.sh` documented an `ALX_KEEP_IMAGE` variable that was never passed, and
  hardcoded `/data/androlinux` where it should have used `$ALX_ROOT`.
- `gui stop` announced "removed the forward" whether or not one existed.
- The build script's `| tail` swallowed a kernel build's exit code, reporting a
  failed build as successful.

---

## H. systemd, and the kernel work

### H1. Android kernels compile out PID namespaces
systemd checks `getpid() == 1` and exits otherwise. Being PID 1 without replacing
Android's init needs a PID namespace, and Google disables it in GKI *on purpose*:
```
arch/x86/configs/gki_defconfig:41:  # CONFIG_PID_NS is not set
```
Samsung's 4.19 does the same. The tablet's kernel exposes only four namespace
types, which is the whole story in one line:
```
$ ls /proc/self/ns/
cgroup  mnt  net  uts            ← no pid, no ipc, no user
$ unshare --pid --fork true
unshare: Invalid argument
```
This is a large part of why Android 15's own "Linux Terminal" uses a VM. It also
rules out Docker and rootless Podman, for the same reason.

### H2. A from-scratch GKI kernel boots but kills adb
`kernel/build-kernel.sh` builds `android15-6.6` with `CONFIG_PID_NS=y`. It compiles,
and the emulator boots it — Android's init runs and starts hundreds of services.
But **adb never leaves `offline`**, because GKI's "generic kernel + vendor modules"
split means a rebuilt kernel rejects every module:
```
init: Failed to insmod '/vendor/lib/modules/goldfish_pipe.ko': Exec format error
```
Most of that list can be made built-in instead. `goldfish_pipe` cannot: it is the
QEMU pipe transport adbd talks over, and the in-tree mainline replacement fails its
version handshake with the emulator host:
```
goldfish_pipe: probe of GFSH0003:00 failed with error -22
WARNING: drivers/platform/goldfish/goldfish_pipe.c:912 goldfish_pipe_probe
```
AOSP's working version is out-of-tree.

### H3. Host toolchain friction
- `certs/extract-cert.c` does not compile against Ubuntu 24.04's OpenSSL 3
  (`use of undeclared identifier 'key_pass'`). Dropping
  `CONFIG_SYSTEM_TRUSTED_KEYRING` removes `certs/` from the build and affects
  neither vermagic nor module CRCs.
- `lz4` missing caused a failure at the *very last* step after ~20 minutes. The
  script now preflights every host tool.

### H4. The vermagic-matched build
`kernel/build-kernel-matched.sh` takes the exact tag (`android15-6.6.50_r00`), the
stock kernel's own `.config` pulled from `/proc/config.gz`, changes exactly one
option, and pins `CONFIG_LOCALVERSION` so the release string is byte-identical:
```
kernelrelease  6.6.50-android15-8-g8adecb593e9b-ab12525588
target         6.6.50-android15-8-g8adecb593e9b-ab12525588
```
`LOCALVERSION=` must be *set but empty*, or `setlocalversion` appends a `+` and the
identity no longer matches. Module signing is not an obstacle either — the stock
config leaves `CONFIG_MODULE_SIG_FORCE` unset.

**Still unresolved.** With matching vermagic, modules are *still* rejected with
`Exec format error` and the kernel logs no diagnostic — no "version magic should
be", no symbol disagreement. Boot then fails earlier, in first-stage init, because
`virtio_blk` is a module in the stock config. Leading suspect is module ABI rather
than identity: the stock config has `CONFIG_CFI_CLANG=y` and
`CONFIG_DEBUG_INFO_BTF=y`, both disabled in these scripts because they are tied to
the clang revision AOSP pins. `include/linux/vermagic.h` confirms CFI is *not* part
of the vermagic string, so this would be an ABI incompatibility. Next step is
building with AOSP's clang prebuilt and keeping CFI on.

### H5. The tablet is a better kernel target than the emulator
```
$ ls /vendor/lib/modules/*.ko | wc -l
0
$ lsmod | wc -l
1
```
Samsung's kernel is effectively monolithic, so the failure mode that killed both
emulator attempts does not apply. Samsung also publishes matching kernel source per
firmware build (`T970XXS7DXH1`), so it is the exact kernel with one option flipped.
systemd is already installed in the rootfs (`252.39`,
`/sbin/init → /lib/systemd/systemd`) and would start the moment a PID namespace
existed.

What makes it harder is delivery, not building: a boot.img repack, a Magisk
re-patch to retain root, and a Download Mode flash of a daily-driver tablet.
**Parked at the owner's request, not abandoned.** A stock firmware image downloaded
first turns a bad flash into a ~20-minute recovery.

---

## I. Ubuntu's actual desktop, without systemd

The first Ubuntu build was fair-but-wrong: the right distro and release, with
**XFCE** on top. Ubuntu's desktop is GNOME with Yaru, the Ubuntu font and the
left-hand dock, and XFCE looks nothing like it. The base was genuinely Ubuntu; the
GUI was a carry-over from the Debian work that nobody had reconsidered. The owner's
verdict — "this is not the actual ubuntu, the gui is completely diff" — was correct.

Getting the real one working turned up four things.

### I1. GNOME's session manager does *not* require systemd
Ubuntu drives `gnome-session` through systemd **user** units, so the expectation was
that GNOME simply could not run here. It can:
```
19153 /usr/libexec/gnome-session-binary --session=ubuntu     ← running
```
`gnome-session` falls back to its own built-in session manager. This was worth
testing rather than reasoning about.

### I2. GNOME Shell dies without a D-Bus *system* bus
`gnome-session` came up and `gnome-shell` immediately went `<defunct>`:
```
accountsservice-WARNING: Failed to connect to the D-Bus daemon
Couldn't connect to system bus: Could not connect: No such file or directory
Gjs-CRITICAL: JS ERROR: Gio.IOErrorEnum ... endSessionDialog.js ... main.js
Execution of main.js threw exception
```
On a normal Ubuntu, systemd starts `dbus.service`. With no systemd, nothing does —
and XFCE never noticed, because it only needs the *session* bus that `dbus-launch`
provides. The fix is to start it by hand before the session:
```sh
ln -sf /etc/machine-id /var/lib/dbus/machine-id   # dbus refuses to start without an id
dbus-daemon --system --fork
```
plus `accounts-daemon` and `polkitd`, which are also systemd units on Ubuntu and
which GNOME asks for the user list and privilege checks. After that:
```
session running (gnome-shell up)
```
GNOME Shell 46 with Yaru, on an Android kernel, with no init system.

### I3. Snaps cannot work here, so Ubuntu's browsers are unusable as shipped
`snapd` requires systemd, so `ubuntu-desktop-minimal` is installed with
`--no-install-recommends` specifically to keep snapd out — verified absent
afterwards. But Ubuntu 24.04 ships Firefox and Chromium *only* as snaps:
```
$ apt-cache show firefox
Version: 1:1snap1-0ubuntu5
Pre-Depends: debconf, snapd (>= 2.54)
```
Checked rather than assumed, for arm64:

| Source | arm64 |
|---|---|
| Mozilla's apt repo | **404** — amd64 only |
| mozillateam PPA | **200** — real deb |
| Mozilla's own tarball | `firefox-153.0.1 linux-aarch64`, 73 MB |

The PPA (pinned above the snap stub) gives a real browser, confirmed by
`about:support`: `Application Binary /usr/lib/firefox/firefox`, `OS Theme Yaru`,
`OS Linux 4.19.113-27114284`.

### I4. Two of my own checks broke once the session stopped being XFCE-and-root
- The readiness check waited for `xfwm4` — XFCE's window manager — so it reported
  a GNOME failure that had not happened, *and* would have hidden a real one. The
  process to wait for is now part of the session definition.
- `gui screenshot` ran as root against a session owned by a user, and X access is
  gated by the Xauthority cookie in that user's home: `xwd: unable to open display
  ':1'`. It now runs as the session owner.
- Then it failed again with `Permission denied` on `/tmp/alx-shot.xwd` — a
  root-owned leftover from an earlier root-run session, in a sticky `/tmp` where an
  ordinary user can neither overwrite nor delete it. Captures now use `mktemp`.

## J. Cloning a GNOME desktop config between machines

With the workstation and the tablet both on **GNOME Shell 46.0**, the desktop
configuration transfers almost exactly. Extensions are JavaScript, so they are
architecture-independent — the host's `~/.local/share/gnome-shell/extensions`
copied straight onto an aarch64 tablet and loaded fine (dash-to-panel v73,
clipboard-indicator, lockkeys).

Settings move with `dconf dump` / `dconf load`, with one operational catch:
**`dconf load` writes through the session bus**, so it must run inside the running
session. The bus address is only discoverable from the session's own process:

```sh
export $(tr '\0' '\n' < /proc/$(pgrep -x gnome-shell)/environ | grep ^DBUS_SESSION_BUS_ADDRESS)
dconf load /org/gnome/shell/ < dc-shell.ini
```

Three things do *not* transfer cleanly, and all three fail by looking broken rather
than by erroring.

### J1. Dark mode switches to a wallpaper key that may point at nothing
Copying `/org/gnome/desktop/interface/` brought `color-scheme='prefer-dark'`, which
makes GNOME read `picture-uri-dark` instead of `picture-uri`. On the tablet that
pointed at `/usr/share/backgrounds/gnome/adwaita-d.jpg` — part of
`gnome-backgrounds`, which `--no-install-recommends` had skipped. A missing file
renders as **plain black**, with no error anywhere. Point it at a wallpaper that
exists, or install the package.

### J2. dash-to-panel stores positions per monitor, keyed by the monitor
The host's value arrived as
`{"NEW-0x00000002":"BOTTOM","DEL-1BFKXN3":"BOTTOM"}` — identifiers for the
workstation's displays, which the tablet is not.

Counter-intuitively, **leaving those foreign keys in place worked** (the panel
appeared at the bottom), while "cleaning up" by resetting the key to `{}` made the
panel vanish entirely. Resetting it was my fix and it was the regression. If the
panel disappears after a config copy, put the value back rather than clearing it.

### J3. Favourites reference apps that may not be installed
`favorite-apps` is a list of `.desktop` filenames. Any that are absent leave dead
slots in the dock. The apply step now filters the list to what exists and reports
what it dropped — `code.desktop`, `sublime_text.desktop` and `tabby.desktop` in
this case.

### And the real constraint: memory, not CPU
GNOME Shell runs, but `free` inside the chroot reports the **shared** kernel's
memory — Android and Linux together:

```
gnome-shell rss:  317 MB
free:             180 MB of 7623 MB     ← with Firefox also open
free:             281 MB                ← after closing Firefox
```

At 180 MB free, the 3840x2160 wallpaper would not render at all; freeing memory
fixed it. Software rendering makes this worse, since textures come out of the same
pool. GNOME plus a browser on a 8 GB tablet shared with a live Android userspace is
genuinely tight — which is the honest argument for `--session xfce` if
responsiveness matters more than matching the workstation pixel for pixel.

## Also worth recording

**The emulator AVD had to be recreated.** The original used a
`google_apis_playstore` system image — a user build where `adb root` is refused, so
nothing here could work. `google_apis` images allow it. The AVD config also had
`disk.dataPartition.path=<temp>`, which makes `/data` volatile and would have
discarded the rootfs on every restart.

**Two things I stated wrongly and later corrected.**
- I said Adreno exposes no `/dev/dri`. It does — `card0` and `renderD128` both
  exist on the Tab S7+. The *conclusion* survived for a more specific reason:
  `card0 → msm_drm` on `qcom,mdss_mdp` is the display controller, while the GPU
  sits behind `/dev/kgsl-3d0`, which Mesa's freedreno cannot drive. So no
  accelerated GL, but the nodes are there.
- I recommended getting Termux:Widget from F-Droid. The device's Termux is the Play
  build (`versionName=googleplay.2025.10.05`); addons must share Termux's signing
  key, so the F-Droid addon will not install alongside it.

**Ubuntu's image parks a user on uid 1000.** `user add` renames the placeholder
rather than shunting the real account to 1001, so uids line up with the workstation
and file ownership matches if files move between them.

---

## The recurring lesson

Nearly every entry above is the same shape: **a check that reported success without
having verified anything.**

- `adb exec-out` returning 0 for a failed command (A1)
- adb returning 0 for a script the device never finished (A7)
- a push reporting success having transferred half the bytes (A6)
- `ls` on a file created by a redirect that never received data (E4)
- a trailing `echo` deciding a script's exit status (E5)
- `pgrep` finding nothing because it was not allowed to look (F2)
- a port probe that could never succeed, so every port looked free (F3)
- a display recorded from a run that never worked, then trusted forever (F4)

The fix is the same each time and is now the house style: **verify the
postcondition, not the exit status.** `install` checks the image and rootfs exist.
`up` checks the mount flags actually took. `push_root` compares sizes.
`gui screenshot` runs `test -s`. `user add` resolves a hostname as the new user
before claiming success. `build-kernel-matched.sh` compares the kernel release
string *before* spending 45 minutes on a build.

The second lesson is narrower but cost the most: **do not do destructive things by
default.** `setenforce 0` rebooted a tablet three times and was never needed;
replacing a live X session destroyed a working desktop on every widget tap. Both
are now opt-in — `--permissive` and `--restart`.
