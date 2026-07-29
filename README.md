# androlinux

Run a full Linux distribution on an Android device's own kernel — no virtual
machine, no syscall translation.

Android *is* Linux. androlinux takes that literally: it puts a real Debian (or
Ubuntu, or Alpine) root filesystem on the device and runs that distro's userspace
directly on the kernel already running there. Binaries execute natively at full
speed, `uname` reports the Android kernel, and `apt` works normally.

```
$ androlinux run 'uname -sr; gcc --version | head -1'
Linux 6.6.50-android15-8-g8adecb593e9b-ab12525588
gcc (Debian 12.2.0-14+deb12u1) 12.2.0
```

Every bug hit while building this, its root cause, the evidence and the fix is
recorded in **[FINDINGS.md](FINDINGS.md)** — worth reading before changing the
device-side scripts, because most of those failures reported success rather than
failing.

## What this is not

- **Not a VM.** Nothing is virtualised. There is no second kernel. Compare
  Android 15's built-in "Linux Terminal", which boots a guest kernel under the
  Android Virtualization Framework.
- **Not proot.** proot intercepts syscalls with `ptrace` — slower, and root
  inside is simulated. androlinux uses real root and real syscalls. (It will
  *fall back* to proot when a device offers nothing better, and says so.)

## Status

Verified end to end on an Android 15 emulator (API 35, x86_64, kernel 6.6.50):

| Capability | State |
| --- | --- |
| Debian 12 userspace, native execution | working |
| Real root (uid 0), working `apt`, DNS | working |
| Compilers / full CLI dev environment | working |
| XFCE desktop over VNC, reachable from the host | working |
| Hardware: sound, FUSE, tun | working |
| Hardware: GPU (`/dev/dri`) | absent on this kernel |
| **systemd** | **blocked by the kernel — see below** |

## The systemd constraint

systemd refuses to start unless it is PID 1. Being PID 1 without replacing
Android's own init requires a PID namespace, and Android kernels commonly compile
that out:

```
$ androlinux probe --raw | grep PID_NS
kcfg.CONFIG_PID_NS=unset

$ adb shell unshare --pid true
unshare: Invalid argument
```

`CONFIG_PID_NS=unset` is not an emulator quirk — it is a common Android kernel
configuration, and it is a large part of why Android's own Linux Terminal resorts
to a VM. On such a kernel, no amount of userspace work produces a PID 1, so
systemd cannot run. `androlinux probe` detects this and selects the `chroot`
strategy, which is everything above minus the init system.

Three ways forward, in increasing cost:

1. **A service manager that does not need PID 1** — `runit`, `s6`, `dinit` or
   `openrc` all supervise services from a plain process. You get managed,
   restartable services; you do not get `systemctl` or unit files.
2. **A kernel with `CONFIG_PID_NS=y`.** See below — attempted, and further than
   it sounds, but not finished.
3. **Bare metal** — postmarketOS or Droidian replace Android outright and boot
   mainline Linux with systemd as the real PID 1.

### The kernel rebuild: how far it got

`kernel/build-kernel.sh` builds an `android15-6.6` GKI kernel with
`CONFIG_PID_NS=y`. It compiles, and the emulator boots it: Android's init runs and
starts hundreds of services. **But adb never leaves `offline`**, which makes the
device unusable.

The cause is GKI's "generic kernel + vendor modules" split. The AVD's modules are
built against its exact kernel, so a rebuilt one rejects every single one:

```
init: Failed to insmod '/vendor/lib/modules/goldfish_pipe.ko': Exec format error
```

For most of that list it does not matter — the build script makes those drivers
built-in instead. `goldfish_pipe` is the exception: it is the QEMU pipe transport
`adbd` communicates over, and the in-tree mainline replacement fails its version
handshake with the emulator host:

```
goldfish_pipe: probe of GFSH0003:00 failed with error -22
WARNING: drivers/platform/goldfish/goldfish_pipe.c:912 goldfish_pipe_probe
```

AOSP's working `goldfish_pipe` is out-of-tree and not in `kernel/common`, so it
cannot simply be built in.

`kernel/build-kernel-matched.sh` takes the better approach: source at the exact
tag (`android15-6.6.50_r00`), the stock kernel's own `.config` pulled from
`/proc/config.gz` on the device, exactly one option changed (`CONFIG_PID_NS=y`),
and `CONFIG_LOCALVERSION` pinned so the release string is byte-identical. It
verifies that identity *before* building, and it does match:

```
kernelrelease  6.6.50-android15-8-g8adecb593e9b-ab12525588
target         6.6.50-android15-8-g8adecb593e9b-ab12525588
```

Module signing is not an obstacle either — the stock config leaves
`CONFIG_MODULE_SIG_FORCE` unset, so a module whose signature does not verify
still loads.

**Still unresolved:** with matching vermagic, modules are *still* rejected with
`Exec format error`, and the kernel logs no diagnostic explaining why — no
"version magic should be", no "disagrees about version of symbol", no "unknown
symbol". Boot now fails earlier, in first-stage init, because `virtio_blk` is a
module in the stock config and without it there is no root filesystem.

The leading suspect is the module ABI rather than vermagic: the stock config has
`CONFIG_CFI_CLANG=y` and `CONFIG_DEBUG_INFO_BTF=y`, both disabled in these scripts
because they are tied to the specific clang revision AOSP pins. Keeping them would
mean building with AOSP's clang prebuilt instead of the distribution's, which is
the next thing to try. Note `include/linux/vermagic.h` confirms CFI is *not* part
of the vermagic string, so this would be an ABI incompatibility, not an identity
mismatch.

Until that is settled, `androlinux` runs the `chroot` strategy, which is
everything in the table above except the init system.

### A physical device is a better kernel target than the emulator

Measured on the Galaxy Tab S7+ (SM-T970, `T970XXS7DXH1`, kernel 4.19):

```
$ ls /proc/self/ns/
cgroup  mnt  net  uts            ← no pid, no ipc, no user
$ unshare --pid --fork true
unshare: Invalid argument

$ ls /vendor/lib/modules/*.ko | wc -l
0                                ← nothing depends on module vermagic
```

systemd is already installed there (`252.39`, `/sbin/init → /lib/systemd/systemd`)
and would start the moment a PID namespace existed. Samsung's kernel is
effectively monolithic, so the failure mode that killed both emulator attempts —
21 vendor modules rejected on vermagic, `goldfish_pipe` fatally among them —
simply does not apply. Samsung also publishes matching kernel source per firmware
build, so it is the *exact* kernel with one option flipped rather than a
from-scratch GKI config.

What makes it harder instead is delivery: it stops being a build problem and
becomes a boot.img repack, a Magisk re-patch to retain root, and a Download Mode
flash of somebody's daily driver. **Parked, not abandoned** — a stock firmware
image downloaded first turns a bad flash into a 20-minute recovery.

## Requirements

- A rooted Android target. Emulator AVDs must use a `google_apis` or `default`
  system image — **`google_apis_playstore` images are user builds and can never
  be rooted**, so `adb root` is refused.
- `adb` on the host (Android platform-tools).
- Python 3.10+. No third-party dependencies.

## Install

```bash
cd androlinux
pip install -e .            # gives you the `androlinux` and `alx` commands
```

Without that, every command below still works as
`python3 -m androlinux.cli <subcommand>` from the project directory.

## From a cold start, on the emulator

The AVD must come from a **`google_apis`** or **`default`** system image.
`google_apis_playstore` images are user builds and `adb root` is refused on them,
so nothing here can work.

```bash
# 1. one-time: a rootable AVD
sdkmanager "system-images;android-35;google_apis;x86_64"
avdmanager create avd -n androlinux -k "system-images;android-35;google_apis;x86_64" -d pixel_7
# then in ~/.android/avd/androlinux.avd/config.ini set:
#   disk.dataPartition.size=32G     (a desktop needs room)
#   hw.ramSize=6144
#   hw.cpu.ncore=4
# and DELETE any 'disk.dataPartition.path=<temp>' line — it makes /data volatile,
# so the rootfs would vanish on every restart.

# 2. boot it (drop -no-window to watch it)
~/Android/Sdk/emulator/emulator -avd androlinux -no-snapshot -no-boot-anim \
    -gpu swiftshader_indirect &
adb wait-for-device
until [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ]; do sleep 2; done

# 3. one-time: check the target, then install Debian and a desktop
androlinux probe
androlinux install debian:bookworm --size 12G
androlinux gui install

# 4. every session
androlinux up
androlinux gui start --geometry 2200x800
androlinux gui viewer          # native app on the device — no browser
```

For a usable view on a phone-shaped screen, rotate to landscape first; a
1280x800 desktop letterboxes badly into 1080x2400 portrait:

```bash
adb shell settings put system accelerometer_rotation 0
adb shell settings put system user_rotation 1
```

When you are done:

```bash
androlinux gui stop
androlinux down                # unmount and release the loop device
adb emu kill
```

`down` before killing the emulator is worth the habit — it unmounts cleanly
instead of leaving the ext4 image dirty.

## Use

```bash
androlinux probe                      # what this target can and cannot do
androlinux install debian:bookworm    # fetch and unpack a rootfs
androlinux up                         # mount it and prepare kernel interfaces
androlinux enter                      # interactive shell inside Debian
androlinux run 'apt-get install -y neovim'
androlinux gui install --session gnome   # Ubuntu's real desktop: GNOME + Yaru
androlinux gui install                # or XFCE, much lighter
androlinux gui start --session gnome  # session on :1, forwarded to localhost:5901
androlinux ssh enable                 # key-only sshd, reachable over wifi and adb
androlinux gui web --on-device        # show the desktop on the phone screen itself
androlinux gui screenshot -o shot.png
androlinux status
androlinux autostart enable --gui     # bring it all up after a reboot (needs Magisk)
androlinux down                       # unmount, release the loop device
```

Start with `probe`. It reports what the kernel actually permits and picks a
strategy it can justify, rather than failing deep inside a bootstrap.

`--serial` is accepted before or after the subcommand, since typing it at the end
is the natural reflex.

## On a real device (phone or tablet)

Nothing here is emulator-specific — the architecture is detected and `aarch64`
maps to `arm64`, so a tablet gets an arm64 rootfs automatically. What changes is
how root is obtained and what is verified.

**The hard requirement is root, and it must be Magisk.** `adb root` only exists on
userdebug/eng builds; retail firmware refuses it. Without root there is no mount
and no `/dev`, and androlinux cannot work — the `proot` fallback is named by
`probe` but is **not implemented**.

Rooting is out of scope here, but be clear-eyed about the cost: it needs an
unlocked bootloader, which **wipes the device**, and it typically trips SafetyNet /
Play Integrity, breaking banking apps and, on Samsung, Knox permanently.

```bash
# 1. Developer options → USB debugging, then plug in and accept the RSA prompt
adb devices                       # must show 'device', not 'unauthorized'

# a tablet is nicer over wifi:
adb tcpip 5555 && adb connect <tablet-ip>:5555

# 2. if the emulator is also running, name the target explicitly
alx probe -s <tablet-serial>

# 3. read the verdict before going further. You want:
#      privilege  uid 0            (via su)
#      exec allowed  ✓
#      mount tmpfs   ✓
#      free space    ≥ 10 GB
#    PID_NS will almost certainly be unset, so the strategy is 'chroot'.

# 4. install — arch is detected, no --arch needed
alx install debian:bookworm --size 12G -s <tablet-serial>
alx up          -s <tablet-serial>
alx gui install -s <tablet-serial>

# 5. match the session to the tablet's panel
adb -s <tablet-serial> shell wm size          # e.g. 2560x1600
alx gui start --geometry 2560x1600 -s <tablet-serial>
alx gui viewer -s <tablet-serial>

# 6. this actually works on a Magisk device, unlike on an emulator
alx autostart enable --gui -s <tablet-serial>
```

### Three things a real device taught us

All three were found on a Magisk-rooted Samsung Galaxy Tab S7+ (SM-T970, Android
13, kernel 4.19) and none of them show up on an emulator.

**1. `setenforce 0` panics a Samsung kernel.** Knox/RKP treats disabling SELinux
enforcement as tampering and reboots the device on the spot. The evidence is a log
synced either side of the call:

```
pre=Enforcing        ← written
                     ← "rc=" and "post=" never appear; the kernel died in between
```

androlinux therefore **never touches SELinux** unless you pass `--permissive`, and
on Samsung you should not. It turned out to be unnecessary anyway: Magisk's `su`
runs in an unconfined domain, so `mount -o suid,dev` and `mknod` both succeed while
enforcing. `probe` tests exactly that, which is why its live tests matter more than
reading kernel config.

**2. apt cannot use the network as its sandbox user.** Android gates socket
creation on group membership (`AID_INET`, 3003), and a chroot process has no
supplementary groups. apt drops to the unprivileged `_apt` user to fetch packages,
which then cannot open a socket — and reports it as a DNS problem:

```
Temporary failure resolving 'deb.debian.org'
```

That message is misleading; resolution works fine as root. Demonstrated directly:

```bash
androlinux run 'getent hosts deb.debian.org'                       # works
androlinux run 'setpriv --reuid=_apt --clear-groups getent hosts deb.debian.org'   # fails
```

`install` now writes `APT::Sandbox::User "root";` into
`/etc/apt/apt.conf.d/90androlinux`.

**3. Do not hardcode the emulator's resolver.** `10.0.2.3` is the emulator's
gateway DNS and is unroutable on a physical device. Listed first it makes every
lookup wait for a timeout; glibc eventually falls through, so `getent` looks fine
while apt gives up first. `resolv.conf` is now rebuilt on every `up` from Android's
own `net.dns*` properties, falling back to public resolvers.

### What is different, and what is untested

**The Magisk `su` path is written but has never been exercised** — this project was
built against an emulator, where root comes from `adb root`. It is the most likely
thing to break. Two places take a different code path when `root_method == "su"`:
every `adb.script` call wraps itself in `su -c 'sh <path>'`, and `gui screenshot`
copies through `/data/local/tmp` because `adb pull` runs unprivileged. If something
misbehaves, keep the scripts and run them by hand:

```bash
ANDROLINUX_KEEP_SCRIPTS=1 alx up -s <serial>
adb -s <serial> shell 'ls -l /data/local/tmp/androlinux/'
adb -s <serial> shell "su -c 'sh /data/local/tmp/androlinux/up-XXXX.sh'"
```

**Encrypted /data.** Real devices use file-based encryption, so `/data` is
unreadable until the first unlock after a reboot — and Magisk's `service.d` fires
before that. The boot hook already waits up to four minutes for the rootfs image to
appear and gives up quietly instead of failing where nobody can see it.

**GPU is still unlikely to help.** Android GPUs expose vendor nodes —
`/dev/kgsl-3d0` on Adreno, `/dev/mali0` on Mali — not the DRM `/dev/dri` that Mesa
needs. `probe` reports what actually exists; expose extra nodes with
`alx up --hw snd,fuse,tun,dri,kgsl-3d0`.

**SELinux.** androlinux sets it permissive while the rootfs runs. Some vendor
kernels refuse `setenforce`; if `probe` shows mount failing while enforcing, that
is why.

**Storage.** A desktop plus toolchain is ~6 GB. The image is sparse, so `--size`
is a ceiling rather than an allocation, but `/data` needs the real headroom.

## Ubuntu's real desktop

`--session gnome` installs and starts GNOME Shell with Yaru, the Ubuntu font and
the dock — Ubuntu's actual desktop, not something themed to look like it. It works
without systemd, which was not obvious:

```bash
androlinux install ubuntu:noble --name ubuntu
androlinux user add b
androlinux gui install --session gnome        # ~990 packages
androlinux gui start   --session gnome --geometry 2560x1600
androlinux gui viewer
```

Three things this needs that XFCE did not:

**A D-Bus system bus, started by hand.** systemd normally starts `dbus.service`.
Without it `gnome-session` runs but `gnome-shell` dies instantly —
`Couldn't connect to system bus` — while XFCE never notices, because it only uses
the session bus `dbus-launch` provides. `gui start` now starts `dbus-daemon
--system`, plus `accounts-daemon` and `polkitd`, before the session.

**A non-root user.** GNOME expects a real home, and several apps refuse to run as
root outright. `user add` first.

**No snaps, so no Ubuntu-packaged browser.** `snapd` needs systemd, so
`ubuntu-desktop-minimal` is installed without recommends to keep snapd out — and
Ubuntu 24.04's `firefox` package is a snap stub that `Pre-Depends: snapd`. For a
real browser on arm64, the mozillateam PPA has debs (Mozilla's own apt repo is
amd64-only; its tarballs do cover `linux-aarch64`):

```bash
androlinux run 'add-apt-repository -y ppa:mozillateam/ppa'
androlinux run 'printf %s\\n "Package: *" "Pin: release o=LP-PPA-mozillateam" \\
  "Pin-Priority: 1001" > /etc/apt/preferences.d/mozilla-firefox'
androlinux run 'apt-get update && apt-get install -y firefox'
```

**Expect it to be slow.** There is no usable GPU — Adreno sits behind
`/dev/kgsl`, which Mesa cannot drive — so GNOME Shell composites in software.
It runs; it is not brisk. XFCE remains the option if responsiveness matters more
than fidelity.

## Reaching it over SSH

```bash
androlinux ssh enable            # authorises ~/.ssh/id_*.pub for the recorded user
ssh -p 8022 b@127.0.0.1         # over adb's forwarder
ssh -p 8022 b@<device-ip>       # over wifi — the chroot shares Android's network
```

Key-only, `PermitRootLogin no`, port 8022. Restarted automatically by `up` after a
reboot, because `/run` is a fresh tmpfs each time and sshd's privilege-separation
directory has to be recreated.

## Several distros at once

Each rootfs is an independent instance with its own image, mount and mountpoint.
The unnamed one keeps the original paths, so adding this feature left an existing
install untouched.

```bash
androlinux install debian:bookworm                    # /data/androlinux/rootfs.img
androlinux install ubuntu:noble --name ubuntu         # /data/androlinux/ubuntu/rootfs.img
androlinux up    --name ubuntu
androlinux enter --name ubuntu
```

`--name` is accepted before or after the subcommand. Images are sparse, so an
idle second instance costs little beyond what it actually stores.

Useful for matching a desktop machine: run the same distro and release on the
tablet as on the workstation, and the only difference left is the architecture.

**Instances cannot share an X display.** They share one network namespace, so the
second server finds the RFB port taken. Each instance is therefore allocated a
display the first time it starts one and remembers it — the first instance gets
`:1`/5901, the next `:2`/5902 — and `viewer`, `web`, `screenshot` and `stop` all
follow the recorded value. Override with `--display` if you want to choose.

Two Android-specific traps are worth knowing if you touch this code, because both
produce confidently wrong behaviour rather than errors:

*`pgrep` cannot see other users' processes.* Android mounts `/proc` with
`hidepid=2`:

```
proc /proc proc rw,relatime,gid=3009,hidepid=2
```

So once the desktop ran as a normal user, a root-side `pgrep -f Xtigervnc`
reported nothing, and the "is a session already running?" check happily started a
second server on a port already in use. Liveness is now decided by connecting to
the RFB port, which is uid-independent.

*Device scripts run in mksh, not bash.* A port probe written with bash's
`/dev/tcp` silently fails for *every* port under Android's shell — so the
"first free display" search returned `:1` every time, including when `:1` was
taken. Port questions are asked of `ss` instead. A remembered display is also
re-validated rather than trusted, because the buggy version recorded a display it
had never successfully started on and every later run inherited that choice.

## A non-root user

Root is where a session must begin — `chroot`, `mount` and `mknod` are privileged
— but it is a poor place to stay: Chromium and Firefox refuse to run as root, and
per-user tooling like pyenv and nvm wants a real home.

```bash
androlinux user add b            # then `enter` uses it by default
androlinux enter                 # as b
androlinux enter --root          # when apt is involved
androlinux run --user b 'whoami'
```

**The reason this needs a command rather than a `useradd`** is that Android decides
network access by *group membership*, not capability. A process must be in
`AID_INET` (gid 3003) to create a socket, and a chroot inherits no Android groups —
so a user created the obvious way has no network at all:

```
setpriv --reuid=1000 --regid=1000 --clear-groups getent hosts deb.debian.org  → FAILS
setpriv --reuid=1000 --regid=1000 --groups=3003  getent hosts deb.debian.org  → RESOLVES
```

`user add` therefore creates groups whose gids match Android's AIDs — 3003 inet,
3004 net_raw so `ping` works, 3005 net_admin, 1015/1023 storage, 9997 everybody —
and then *proves* the account can resolve before reporting success. It is the same
mechanism that made apt fail with `Temporary failure resolving` while root was
fine.

It also takes over uid 1000 when a distro image has parked a placeholder there
(Ubuntu's images ship an `ubuntu` user), renaming it rather than shunting the real
account to 1001, so uids line up with the workstation.

`sudo` works because the image is mounted `suid` — one of the reasons for the
loopback ext4 rather than living on `/data` directly.

### What will not work in there

**Docker.** It needs PID and user namespaces, and Android kernels compile both
out — the same wall as systemd. `/proc/self/ns/` on the Tab S7+ lists only
`cgroup mnt net uts`. Podman in rootless mode fails for the same reason.

## Starting it from the device itself

After `autostart enable`, the device is self-sufficient — it needs no host to come
back up. Two ways in:

**It already starts at boot.** Reboot and the rootfs is mounted and the desktop
running before you have finished unlocking. Only the viewer needs opening.

**When you have stopped it,** run this from any terminal app on the device
(Termux, for instance):

```bash
su -c 'sh /data/androlinux/start'
```

which prints

```
androlinux: starting
  rootfs: mounted
  desktop: running
  viewer: opened vnc://127.0.0.1:5901
androlinux: ready
```

It mounts the rootfs, starts the desktop, waits for the X server to actually
appear, and opens the viewer. If a step fails it says which one and points at
`/data/androlinux/boot.log` rather than reporting success.

**Running it again while the desktop is up does not disturb it.** That matters
because the home-screen widget runs this on every tap, and most taps happen when
the session is already there. It short-circuits to just raising the viewer:

```
androlinux: starting
  already running — opening the viewer
androlinux: ready
```

This was originally wrong in a destructive way: the desktop start treated an
existing X socket as stale and killed the server to replace it, so a second tap
tore down every open window and dropped the connected viewer — indistinguishable
from the app crashing. A live session is now left alone at two levels: `start`
short-circuits before reaching it, and `gui-start.sh` refuses to replace a running
server unless `ALX_FORCE=1`.

When you genuinely want a fresh session — a different geometry, say — ask for it:

```bash
androlinux gui start --restart --geometry 2560x1600
```

The first `su` will raise a Magisk prompt; grant it *Forever* or the script is
denied whenever the screen is locked.

### An alias

```bash
# in Termux
cat >> ~/.bashrc <<'EOF'
alias alx="su -c 'sh /data/androlinux/start'"
alias alx-stop="su -c 'sh /data/androlinux/chroot-exec /usr/bin/vncserver -kill :1'"
alias alx-log="su -c 'tail -30 /data/androlinux/boot.log'"
EOF
```

Reopen Termux and `alx` starts everything.

### One tap from the home screen

```bash
mkdir -p ~/.shortcuts
printf '#!/data/data/com.termux/files/usr/bin/sh\nsu -c "sh /data/androlinux/start"\n' \
  > ~/.shortcuts/androlinux
chmod 700 ~/.shortcuts/androlinux
```

Then install **Termux:Widget** and drop its widget on a home screen.

**Get the addon from the same store as Termux itself.** Termux and its addons must
share a signing key, and the Play Store and F-Droid builds are signed differently —
installing the F-Droid addon next to a Play Store Termux fails outright. Check
which you have:

```bash
adb shell 'pm list packages -i | grep com.termux'   # installer=com.android.vending → Play
adb shell 'dumpsys package com.termux | grep versionName'  # "googleplay.…" → Play
```

## When something goes wrong

**`gui start`: "the X server never created /tmp/.X11-unix/X1"**
An old `Xtigervnc` still holds display :1 and port 5901. androlinux refuses to
unlink its socket rather than stranding it. Check and clear by hand:

```bash
androlinux run 'pgrep -a Xtigervnc; cat /root/.vnc/*.pid'
androlinux run 'vncserver -kill :1'
```

**`adb devices` shows `offline` and never recovers**
Almost always a custom kernel. `kernel/build-kernel.sh` produces a kernel whose
vendor modules will not load, including `goldfish_pipe` — the transport adbd
speaks over. Boot without `-kernel` to get back.

**`gui screenshot` used to write a 0-byte PNG.** It now fails loudly instead. If
it reports a dead display, the session is not running — `androlinux gui start`.

**The desktop is tiny or letterboxed**
Match the session to the screen: rotate to landscape and start at `2200x800`.
Portrait 1080x2400 will never fit a 4:3-ish desktop well.

**`install` printed "done" but nothing is there**
If the device disconnected or rebooted part-way through, adb returns the output it
had already received and **exits 0** — so a script that never finished looks
successful. `install` now verifies the image and rootfs exist on the device
afterwards rather than trusting the exit status, and fails loudly instead.

A tablet browning out is a common cause: a USB-A host port supplies ~500 mA, which
is less than a large tablet draws while unpacking a rootfs. Use a proper charger
and connect over wireless debugging instead:

```bash
adb pair <ip>:<pair-port>      # Developer options → Wireless debugging
adb connect <ip>:<port>
```

**A push fails with "remote couldn't create file: Permission denied"**
`adb push` is performed by adbd, which on a user build runs as the *shell* user —
`su` root does not change that, so it cannot write under `/data/androlinux`. Use
`Adb.push_root`, which stages into `/data/local/tmp` and moves the file into place
as root (a rename, since both are on `/data`). Note adb still prints a misleading
"1 file pushed" summary after the failure.

**The viewer app says "missing server info"**
It was launched at the wrong activity. The activity that *handles* a `vnc://` URI
(AVNC's `UriReceiverActivity`) is not the one left in the foreground afterwards
(`ui.vnc.VncActivity`), and starting the latter directly gives it no connection
details. `gui viewer` now resolves the handler at runtime:

```bash
adb shell "cmd package resolve-activity -a android.intent.action.VIEW -d 'vnc://127.0.0.1:5901'"
```

If you are launching by hand, pass no `-n` at all and let Android resolve it.

**A viewer connects but shows a black screen**
The X server is up but the session died. Check the session log:

```bash
androlinux run 'tail -30 /root/.vnc/*.log'
```

## Seeing it on the device screen

```bash
androlinux up
androlinux gui start --geometry 2200x800
androlinux gui web --on-device
```

That last command serves the session over HTTP from inside the rootfs and opens
the device's own browser at it, so the Linux desktop appears **on the Android
screen** — no viewer app to install, and the same three commands work on a phone.

It works because the chroot shares Android's network namespace: androlinux uses no
network namespace, so a socket bound inside the rootfs is reachable at the device's
own loopback address, both by Android apps and by `adb forward`.

The proxy binds `127.0.0.1` only. Binding `0.0.0.0` would combine with the
session's `SecurityTypes None` to expose an unauthenticated desktop to anything
that can reach the device's IP. Loopback is enough for both consumers — the
browser runs on the device, and adb's forwarder also connects from inside it.

Two details worth knowing if you build on this:

- The URL uses `127.0.0.1`, not `localhost`. websockify binds IPv4 only, while
  Android resolves `localhost` to `::1` as well, so the name can cost a refused
  connection before Chrome falls back.
- The desktop is letterboxed unless its aspect ratio roughly matches the screen.
  Rotate to landscape (`adb shell settings put system user_rotation 1`) and start
  the session at something like `2200x800`.

## How it works

**The rootfs lives in a loopback ext4 image, not directly on `/data`.** This is
not tidiness. `/data` is mounted `nosuid,nodev`:

```
/data ext4 rw,seclabel,nosuid,nodev,noatime,...
```

A rootfs unpacked there would break in two quiet ways: every setuid binary
(`sudo`, `su`, `ping`) would lose its privilege bit, and device nodes under the
rootfs's own `/dev` would not function. An ext4 image gets its own mount options,
so it is mounted `suid,dev,exec` and behaves like a normal Linux root. `up`
verifies the flags actually took, and aborts rather than handing over a subtly
broken system.

**`/dev` is built, not borrowed.** Bind-mounting Android's `/dev` would leak every
`mkdir` the distro does into Android's live `/dev` and hand the rootfs every node
on the device. Instead `up` mounts a fresh tmpfs and creates exactly the standard
nodes, plus `devpts` and `shm`. Hardware exposure is then an explicit choice:

```bash
androlinux up --hw snd,dri,fuse,tun,input
```

Device *files* are recreated with `mknod` at the same major/minor rather than
bind-mounted, because Android's toybox `mount` sees a non-directory source and
tries to attach it as a loop device (`losetup: Invalid argument`). Directories
like `/dev/snd` are bind-mounted, which does work.

**SELinux is set permissive while the rootfs runs.** Android's policy has no
labels for a foreign userspace, so an enforcing kernel denies much of what the
distro does. This is a real reduction in the device's isolation, and androlinux
prints it rather than doing it quietly.

**The desktop is an X server serving RFB, not a window on Android.** Android's
SurfaceFlinger owns the display, GPU and input devices; a second X server cannot
take them without displacing Android. Xvnc renders to a framebuffer instead, which
is reachable from a viewer on the device or forwarded to the host over adb, and
behaves the same on an emulator and a phone. It binds loopback only — `adb
forward` connects from inside the device, so the desktop is never exposed on the
device's network.

### Notes on the transport

Shell work is pushed to the target as a file and run by path. Three measured
properties of adb 34 force this:

- `adb exec-out` discards exit status — it returns 0 even for `exit 7`.
- `adb exec-out sh` ignores piped stdin; it opens an interactive shell and hangs.
- adb flattens argv and the device shell re-splits it, so quotes and newlines in
  a large script are unsafe.

`adb shell` with a command argument propagates the real exit status and emits
LF-only output, so that is the execution path; pushing the body as a file removes
every quoting layer between host and device shell.

## Layout

```
androlinux/
  adb.py          transport: root acquisition, script delivery, port forwarding
  probe.py        capability detection and the strategy decision
  rootfs.py       image download, xz→gz recompression, install
  container.py    up / down / run / enter — native execution on the Android kernel
  gui.py          desktop session lifecycle
  config.py       paths and layout
  scripts/        the shell that runs on the target
```

`scripts/` is POSIX `sh` for Android's mksh/toybox — no arrays, no `[[ ]]`, no
`local`. `probe.sh` tests load-bearing capabilities by attempting them rather than
inferring from kernel config, because config symbols and reality disagree often
enough to matter.
