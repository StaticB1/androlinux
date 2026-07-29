"""Target capability probing, and the strategy decision that follows from it.

Running a foreign distro's userspace on an Android kernel is not uniformly
possible — it depends on kernel build options, mount flags and SELinux policy
that vary between devices. Rather than guess and fail deep inside a bootstrap,
androlinux interrogates the target first and picks a strategy it can justify.

The strategies, strongest to weakest:

``systemd``
    Root, mounts, and a PID namespace. The distro's real init runs as PID 1 on
    the Android kernel. Units, timers and journald all work. No virtualisation
    and no syscall translation.
``chroot``
    Root and mounts, but no usable PID namespace. The full native rootfs runs
    and binaries execute at native speed, but nothing can be PID 1, so services
    are launched directly instead of by an init system.
``proot``
    No root. ptrace-based syscall interception in userspace. Works anywhere but
    is slower and fakes privilege, so it is a fallback, not a goal.
``blocked``
    Something fatal — most often ``/data`` mounted ``noexec``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .adb import Adb

_SCRIPT = Path(__file__).parent / "scripts" / "probe.sh"

# Android reports the CPU one way, Debian names its ports another.
_DEBIAN_ARCH = {
    "x86_64": "amd64",
    "aarch64": "arm64",
    "armv8l": "arm64",
    "armv7l": "armhf",
    "i686": "i386",
}


@dataclass
class Verdict:
    strategy: str
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def viable(self) -> bool:
        # Blockers matter even when a strategy was named. An unrooted target
        # resolves to "proot", which is not implemented — reporting that as viable
        # would let `install` start and then fail at the first mount.
        return self.strategy != "blocked" and not self.blockers


@dataclass
class Capabilities:
    """Parsed ``key=value`` output from ``scripts/probe.sh``."""

    raw: dict[str, str]

    def get(self, key: str, default: str = "") -> str:
        return self.raw.get(key, default)

    def yes(self, key: str) -> bool:
        return self.raw.get(key, "").strip().lower() in {"yes", "1", "true", "pass"}

    def has_bin(self, name: str) -> bool:
        return bool(self.raw.get(f"bin.{name}", "").strip())

    @property
    def complete(self) -> bool:
        """False if the probe was truncated — a partial report must not be trusted."""
        return self.raw.get("probe.done") == "1"

    @property
    def is_root(self) -> bool:
        return self.get("id.uid") == "0"

    @property
    def debian_arch(self) -> str:
        return _DEBIAN_ARCH.get(self.get("kernel.arch"), "")

    @property
    def data_free_gb(self) -> float:
        try:
            return int(self.get("data.free_kb", "0")) / 1024 / 1024
        except ValueError:
            return 0.0

    # ------------------------------------------------------------------ verdict

    def decide(self) -> Verdict:
        v = Verdict("blocked")

        if self.get("test.data_exec") == "fail":
            v.blockers.append(
                "/data is noexec — binaries there cannot run, so no rootfs can live on it. "
                "A loopback ext4 image mounted with exec is the usual way around this."
            )
            return v

        if not self.is_root:
            v.strategy = "proot"
            v.reasons.append("no root, so the kernel will not grant mounts or namespaces")
            v.blockers.append(
                "the proot strategy is NOT IMPLEMENTED yet, so androlinux cannot run on this "
                "target. Root it (Magisk on a physical device) or use an emulator AVD built "
                "from a 'google_apis'/'default' system image, where 'adb root' works."
            )
            v.warnings.append(
                "for reference, proot would intercept syscalls with ptrace: slower than a "
                "chroot, and root inside it is simulated rather than real"
            )
            return v

        v.reasons.append("uid 0 on the target")

        if not self.yes("test.mount_tmpfs"):
            v.blockers.append(
                "cannot mount even a tmpfs as root — without mounts the rootfs gets no "
                "/proc, /sys or /dev. Check whether SELinux is enforcing."
            )
            return v
        v.reasons.append("mount works")

        pid_ns = self.get("test.pid_ns")
        if pid_ns == "pass":
            v.strategy = "systemd"
            v.reasons.append("PID namespace confirmed: a child can be PID 1, so systemd can boot")
        elif pid_ns == "no-unshare":
            if self.yes("ns.pid"):
                v.strategy = "systemd"
                v.reasons.append(
                    "kernel exposes /proc/self/ns/pid but Android has no unshare(1); "
                    "androlinux will supply a launcher"
                )
                v.warnings.append("PID namespace not yet proven — it is verified during 'start'")
            else:
                v.strategy = "chroot"
                v.reasons.append("no PID namespace support detected; systemd cannot be PID 1")
        else:
            v.strategy = "chroot"
            v.reasons.append(f"PID namespace test failed ({pid_ns}); running without an init")

        if self.get("selinux.mode", "").lower() == "enforcing":
            v.warnings.append(
                "SELinux is enforcing; androlinux sets it permissive while the rootfs runs, "
                "since Android policy has no labels for a foreign userspace"
            )
        if self.get("cgroup.version") == "none":
            v.warnings.append("no cgroup hierarchy — systemd resource control will be inert")
        elif self.get("cgroup.version") == "v1":
            v.warnings.append(
                "cgroup v1 only; modern systemd expects the unified (v2) hierarchy"
            )
        if not self.debian_arch:
            v.warnings.append(f"unrecognised CPU {self.get('kernel.arch')!r}; set --arch by hand")
        if self.data_free_gb < 6:
            v.warnings.append(
                f"only {self.data_free_gb:.1f} GB free on /data — a desktop needs roughly 6 GB"
            )
        return v


def run(adb: Adb) -> Capabilities:
    """Execute the probe on the target and parse it.

    The whole probe is one shell program delivered over stdin, so it costs a
    single adb round trip instead of one per fact.
    """
    script = _SCRIPT.read_text()
    res = adb.script(script, root=_can_root(adb), timeout=180, label="probe")

    raw: dict[str, str] = {}
    for line in res.out.splitlines():
        if "=" in line:
            k, _, val = line.partition("=")
            raw[k.strip()] = val.strip()
    if not raw:
        raise RuntimeError(
            f"probe produced no output (exit {res.code}).\n"
            f"stderr: {res.err.strip() or '<empty>'}"
        )
    return Capabilities(raw)


def _can_root(adb: Adb) -> bool:
    try:
        adb.acquire_root()
        return True
    except Exception:
        return False


# ------------------------------------------------------------------- rendering

_MARKS = {True: "✓", False: "✗"}


def render(caps: Capabilities, verdict: Verdict) -> str:
    g = caps.get
    out: list[str] = []
    add = out.append

    add("")
    add(f"  target      {g('android.device') or '?'}  ·  Android {g('android.release')} "
        f"(API {g('android.sdk')}, {g('android.build')} build)")
    add(f"  kernel      Linux {g('kernel.release')}  {g('kernel.arch')}"
        + (f"  → debian/{caps.debian_arch}" if caps.debian_arch else ""))
    add(f"  privilege   uid {g('id.uid')}"
        f"  ·  SELinux {g('selinux.mode') or 'unknown'}")
    add("")

    add("  filesystem")
    add(f"    /data          {g('mount.data.fstype')}  [{g('mount.data.opts')}]")
    add(f"    exec allowed   {_MARKS[caps.get('test.data_exec') == 'pass']}"
        "   (fatal if absent — the rootfs lives here)")
    add(f"    free space     {caps.data_free_gb:.1f} GB")
    add("")

    add("  namespaces")
    for n in ("pid", "mnt", "net", "uts", "ipc", "user", "cgroup"):
        add(f"    {n:<14} {_MARKS[caps.yes(f'ns.{n}')]}")
    add("")

    add("  live tests")
    for label, key, note in (
        ("mount tmpfs", "test.mount_tmpfs", "needed for /proc, /dev, /run"),
        ("mknod", "test.mknod", "needed for device nodes in the rootfs"),
        ("PID namespace", "test.pid_ns", "needed for systemd as PID 1"),
    ):
        val = caps.get(key, "n/a")
        add(f"    {label:<14} {_MARKS[val == 'pass']}  {val:<18} {note}")
    add("")

    add(f"  cgroups       {g('cgroup.version')}")
    ctrl = g("cgroup.controllers")
    if ctrl:
        add(f"    controllers  {ctrl.rstrip(',')}")
    add("")

    present = [b for b in ("unshare", "nsenter", "chroot", "mount", "losetup", "mknod",
                           "tar", "gzip", "xz", "busybox", "setenforce")
               if caps.has_bin(b)]
    missing = [b for b in ("unshare", "nsenter", "chroot", "mount", "losetup", "mknod",
                           "tar", "gzip", "xz", "busybox", "setenforce")
               if not caps.has_bin(b)]
    add("  tooling on target")
    add(f"    present      {', '.join(present) or '—'}")
    add(f"    missing      {', '.join(missing) or '—'}")
    add("")

    hw = [d for d in caps.raw if d.startswith("dev./dev/")]
    have_hw = [d.removeprefix("dev.") for d in sorted(hw) if caps.yes(d)]
    add("  hardware nodes")
    add(f"    reachable    {', '.join(have_hw) or '—'}")
    add("")

    add(f"  ── verdict: {verdict.strategy.upper()} "
        f"{'─' * max(0, 44 - len(verdict.strategy))}")
    for r in verdict.reasons:
        add(f"     · {r}")
    for w in verdict.warnings:
        add(f"     ! {w}")
    for b in verdict.blockers:
        add(f"     ✗ {b}")
    add("")
    return "\n".join(out)
