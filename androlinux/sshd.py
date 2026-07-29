"""Reaching the rootfs over SSH.

The chroot shares Android's network namespace, so an sshd started inside it
listens on the device's real interfaces. That is what makes the tablet usable as a
build box from a workstation — eight Snapdragon cores that would otherwise idle —
and also why this is configured key-only and non-root by default.

The port is additionally forwarded over adb, which works whether or not the two
machines are on the same network.
"""

from __future__ import annotations

from pathlib import Path

from . import config, container, users
from .adb import Adb, AdbError

_SCRIPTS = Path(__file__).parent / "scripts"

DEFAULT_PORT = 8022


def _host_pubkey(explicit: str | None) -> str:
    """The public key to authorise, read from the host."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise AdbError(f"no such public key: {path}")
        return path.read_text().strip()

    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        candidate = Path.home() / ".ssh" / name
        if candidate.is_file():
            return candidate.read_text().strip()

    raise AdbError(
        "no SSH public key found on this host (~/.ssh/id_ed25519.pub or similar).\n"
        "  Generate one with:  ssh-keygen -t ed25519\n"
        "  Or pass --key <path>. Password auth is deliberately disabled, so a key\n"
        "  is the only way in."
    )


def enable(
    adb: Adb,
    *,
    port: int = DEFAULT_PORT,
    user: str | None = None,
    key: str | None = None,
    forward: bool = True,
) -> None:
    """Configure and start sshd in the rootfs, and forward its port."""
    if not container.is_up(adb):
        container.up(adb, quiet=True)

    account = user or users.default(adb)
    if not account:
        raise AdbError(
            "no user to log in as. Create one first:  androlinux user add <name>\n"
            "  Root login over SSH is disabled on purpose; use sudo from the account."
        )

    pubkey = _host_pubkey(key)

    body = config.preamble(
        ALX_PORT=str(port),
        ALX_USER=account,
        ALX_PUBKEY=pubkey,
    ) + (_SCRIPTS / "ssh-enable.sh").read_text()

    res = container.run(adb, body, timeout=600)
    print("\n".join(f"  {ln}" for ln in res.lines()))
    if not res.ok:
        raise AdbError(f"could not enable sshd (exit {res.code}):\n{res.err.strip()}")

    if forward:
        adb.forward(port, port).check(f"forwarding port {port}")
        print(f"  forwarded localhost:{port} → device:{port}")
        print(f"\n  over adb:  ssh -p {port} {account}@127.0.0.1")

    ip = adb.sh("ip route get 1.1.1.1 2>/dev/null | head -1", root=True, timeout=60).out
    addr = ""
    for token in ip.split():
        if token.count(".") == 3 and not token.startswith("1.1.1"):
            addr = token
    if addr:
        print(f"  over wifi: ssh -p {port} {account}@{addr}")
    print()


def status(adb: Adb, *, port: int = DEFAULT_PORT) -> str:
    out = [""]
    enabled = adb.sh(f"test -f {config.MOUNT}/etc/androlinux-ssh && echo yes",
                     root=True, timeout=60).out.strip()
    out.append(f"  ssh            {'enabled' if enabled == 'yes' else 'not enabled'}")

    listening = adb.sh(f"ss -ltn 2>/dev/null | grep -c ':{port}'", root=True, timeout=60)
    running = listening.out.strip() not in ("", "0")
    out.append(f"  sshd           {'listening on ' + str(port) if running else 'not listening'}")
    account = users.default(adb)
    out.append(f"  account        {account or '(none)'}")
    out.append("")
    return "\n".join(out)
