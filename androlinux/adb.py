"""ADB transport for androlinux.

Everything androlinux does on a target goes through here. The design is dictated
by three measured properties of adb 34 rather than by preference:

**``adb exec-out`` discards exit status.** It returns 0 even for ``exit 7`` or
``false``. ``adb shell`` with a command argument propagates the real status and —
contrary to the usual warning about pty translation — emits LF-only output when
it is not interactive. So ``adb shell`` is the execution path; ``exec-out`` is
kept only for pulling binary streams, where exit status does not matter.

**``adb exec-out sh`` ignores piped stdin.** It opens an interactive shell and
hangs waiting on a terminal. Feeding scripts over stdin is therefore not an
option.

**adb re-splits argv on the remote side.** A command assembled into an argv is
flattened to a string and re-parsed by the device's shell, so embedded quotes,
newlines and ``$`` in a large script are unsafe.

The consequence: non-trivial shell is **pushed as a file** and run by path. A
rootfs bootstrap is hundreds of lines of nested quoting; delivering it as a file
removes every quoting layer between here and the device shell, and leaves the
exact script on the device for inspection when something fails.

**Root acquisition differs by target.** Emulators and userdebug builds hand root
over via ``adb root``, which restarts adbd as uid 0. Retail firmware refuses that
and needs Magisk's ``su``. We settle which applies once, then reuse it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass

#: Scratch directory on the target for pushed scripts.
DEVICE_TMP = "/data/local/tmp/androlinux"


class AdbError(RuntimeError):
    """An adb invocation failed, or no usable target was found."""


def _assign(**variables: str) -> str:
    """Render shell assignments, so paths never pass through adb's argv splitting.

    Kept local rather than imported from config to keep this module free of
    intra-package dependencies.
    """
    return "".join(
        f"{key}='{str(value).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
        for key, value in variables.items()
    )


@dataclass(frozen=True)
class Result:
    """Outcome of one command run on the target."""

    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def lines(self) -> list[str]:
        return [ln for ln in self.out.splitlines() if ln.strip()]

    def check(self, what: str) -> "Result":
        if not self.ok:
            detail = (self.err.strip() or self.out.strip() or "<no output>")[-2000:]
            raise AdbError(f"{what} failed (exit {self.code}):\n{detail}")
        return self


@dataclass(frozen=True)
class Device:
    serial: str
    state: str

    @property
    def is_emulator(self) -> bool:
        return self.serial.startswith("emulator-")


class Adb:
    """A handle on one Android target.

    ``root_method`` is ``None`` until :meth:`acquire_root` runs; afterwards it is
    ``"adbd"`` (adbd itself is uid 0, so no wrapper is needed) or ``"su"``.
    """

    def __init__(self, serial: str | None = None, *, binary: str = "adb") -> None:
        exe = shutil.which(binary)
        if exe is None:
            raise AdbError(f"{binary!r} not found on PATH — install android platform-tools")
        self.exe = exe
        self.serial = serial
        self.root_method: str | None = None
        self._tmp_ready = False

    # ---------------------------------------------------------------- plumbing

    def _argv(self, *args: str) -> list[str]:
        argv = [self.exe]
        if self.serial:
            argv += ["-s", self.serial]
        return argv + list(args)

    def raw(self, *args: str, timeout: float | None = 120) -> Result:
        """Run a bare adb subcommand (``devices``, ``push``, ``root``, ...)."""
        try:
            proc = subprocess.run(
                self._argv(*args), capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return Result(124, "", f"timed out after {timeout}s: adb {' '.join(args)}")
        return Result(proc.returncode, proc.stdout, proc.stderr)

    # ------------------------------------------------------------- discovery

    def devices(self) -> list[Device]:
        res = self.raw("devices", timeout=30)
        found = []
        for line in res.out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                found.append(Device(parts[0], parts[1]))
        return found

    def require_device(self) -> Device:
        """Pick the target to operate on, or explain why we cannot."""
        ready = [d for d in self.devices() if d.state == "device"]
        if not ready:
            others = self.devices()
            if others:
                detail = ", ".join(f"{d.serial} ({d.state})" for d in others)
                raise AdbError(f"no ready target; adb sees: {detail}")
            raise AdbError("no Android target connected — boot an emulator or plug in a device")
        if self.serial is None:
            if len(ready) > 1:
                names = ", ".join(d.serial for d in ready)
                raise AdbError(f"several targets connected ({names}) — pass --serial")
            self.serial = ready[0].serial
        return ready[0]

    # ------------------------------------------------------------------ shell

    def sh(self, command: str, *, root: bool = False, timeout: float | None = 120) -> Result:
        """Run one simple command on the target.

        Only for short commands free of quotes, newlines and shell metacharacters
        that adb's remote re-splitting would damage — ``id -u``, ``getprop x``.
        Use :meth:`script` for anything larger.
        """
        if root:
            self._ensure_root()
            if self.root_method == "su":
                return self.raw("shell", "su", "-c", command, timeout=timeout)
        return self.raw("shell", command, timeout=timeout)

    def script(
        self,
        body: str,
        *,
        root: bool = False,
        timeout: float | None = 600,
        label: str = "script",
    ) -> Result:
        """Push a shell program to the target and run it by path.

        Returns its real exit status. The pushed copy is removed afterwards unless
        ``ANDROLINUX_KEEP_SCRIPTS`` is set, which is useful when debugging a
        bootstrap that fails partway.
        """
        if root:
            self._ensure_root()
        self._ensure_tmp()

        name = f"{label}-{uuid.uuid4().hex[:8]}.sh"
        remote = f"{DEVICE_TMP}/{name}"

        local_dir = tempfile.mkdtemp(prefix="androlinux-")
        local = os.path.join(local_dir, name)
        try:
            # A trailing newline matters: mksh silently drops an unterminated
            # final line.
            with open(local, "w", newline="\n") as fh:
                fh.write(body if body.endswith("\n") else body + "\n")

            push = self.raw("push", local, remote, timeout=120)
            if not push.ok:
                raise AdbError(f"could not push {label} to target:\n{push.err.strip()}")

            if root and self.root_method == "su":
                # adb flattens argv and the device shell re-splits it, so passing
                # ("su", "-c", "sh <path>") arrives as four words and su sees only
                # "sh" as its command. Send one pre-quoted string instead, so the
                # remote shell reassembles the argument for su. The path is
                # generated here and contains no quotes or spaces.
                res = self.raw("shell", f"su -c 'sh {remote}'", timeout=timeout)
            else:
                res = self.raw("shell", "sh", remote, timeout=timeout)
            return res
        finally:
            if not os.environ.get("ANDROLINUX_KEEP_SCRIPTS"):
                self.raw("shell", "rm", "-f", remote, timeout=30)
            shutil.rmtree(local_dir, ignore_errors=True)

    def _ensure_tmp(self) -> None:
        if not self._tmp_ready:
            self.raw("shell", "mkdir", "-p", DEVICE_TMP, timeout=30)
            self._tmp_ready = True

    def uid(self) -> str:
        return self.sh("id -u", timeout=30).out.strip()

    # ------------------------------------------------------------------- root

    def _ensure_root(self) -> None:
        if self.root_method is None:
            self.acquire_root()

    def acquire_root(self) -> str:
        """Obtain uid 0 on the target. Returns the method used.

        ``adb root`` is tried first: when it works it removes the ``su`` wrapper
        from every later call. It exists only on userdebug/eng builds, so retail
        firmware and Play Store emulator images fall through to Magisk.
        """
        if self.uid() == "0":
            self.root_method = "adbd"
            return self.root_method

        res = self.raw("root", timeout=60)
        combined = (res.out + res.err).lower()
        if "cannot run as root" not in combined and "not permitted" not in combined:
            # adbd is restarting, so the connection drops briefly.
            self.raw("wait-for-device", timeout=60)
            for _ in range(20):
                if self.uid() == "0":
                    self.root_method = "adbd"
                    return self.root_method
                time.sleep(0.5)

        if self.sh("su -c id -u", timeout=30).out.strip() == "0":
            self.root_method = "su"
            return self.root_method

        raise AdbError(
            "cannot obtain root on this target.\n"
            "  'adb root' was refused and no working 'su' was found.\n"
            "  Play Store emulator images (tag google_apis_playstore) are user builds and\n"
            "  can never be rooted — recreate the AVD from a 'google_apis' or 'default'\n"
            "  image, or root the physical device with Magisk."
        )

    # ------------------------------------------------------------------ files

    def push(self, local: str, remote: str, *, timeout: float | None = 1800) -> Result:
        return self.raw("push", local, remote, timeout=timeout)

    def push_root(self, local: str, remote: str, *, timeout: float | None = 1800) -> Result:
        """Push a file to a destination only root can write.

        ``adb push`` is performed by adbd, which on a user build runs as the
        *shell* user — obtaining root through ``su`` does not change that. Pushing
        straight to /data/androlinux therefore fails with::

            adb: error: failed to copy ...: remote couldn't create file: Permission denied

        (Confusingly, adb still prints a "1 file pushed" summary afterwards.)

        So stage into /data/local/tmp, which shell can write, then move the file
        into place as root. Both live on /data, so the move is a rename and costs
        nothing regardless of file size.

        When adbd itself is root — an emulator or a userdebug build — this is a
        plain push.
        """
        if self.root_method is None:
            self.acquire_root()
        if self.root_method != "su":
            return self.push(local, remote, timeout=timeout)

        self._ensure_tmp()
        staged = f"{DEVICE_TMP}/push-{uuid.uuid4().hex[:8]}"

        pushed = self.push(local, staged, timeout=timeout)
        if not pushed.ok:
            return pushed

        # Verify the whole file arrived. A target that drops off the bus mid-push
        # leaves a truncated file, and adb does not always report that as an
        # error — a 163 MB push was observed landing as 80 MB with no failure.
        want = os.path.getsize(local)
        got = self.sh(f"stat -c %s {staged} 2>/dev/null", timeout=120).out.strip()
        if got != str(want):
            self.sh(f"rm -f {staged}", timeout=60)
            return Result(
                1, "",
                f"push was truncated: {got or '0'} of {want} bytes arrived. "
                "The target most likely disconnected or rebooted mid-transfer.",
            )

        moved = self.script(
            _assign(ALX_SRC=staged, ALX_DST=remote)
            + 'set -e\n'
              'mkdir -p "$(dirname "$ALX_DST")"\n'
              'mv -f "$ALX_SRC" "$ALX_DST"\n',
            root=True, timeout=600, label="push-move",
        )
        if not moved.ok:
            self.sh(f"rm -f {staged}", timeout=60)
        return moved

    def pull(self, remote: str, local: str, *, timeout: float | None = 1800) -> Result:
        return self.raw("pull", remote, local, timeout=timeout)

    def forward(self, local_port: int, remote_port: int) -> Result:
        """Expose a device port on the host, to reach the Linux side's ssh or VNC."""
        return self.raw("forward", f"tcp:{local_port}", f"tcp:{remote_port}", timeout=30)

    # ------------------------------------------------------------------- boot

    def wait_for_boot(self, *, timeout: float = 300) -> bool:
        """Block until Android has finished booting, not merely until adb connects."""
        self.raw("wait-for-device", timeout=timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.sh("getprop sys.boot_completed", timeout=20).out.strip() == "1":
                return True
            time.sleep(2)
        return False
