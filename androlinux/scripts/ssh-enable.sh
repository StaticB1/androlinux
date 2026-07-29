#!/bin/bash
# Set up sshd inside the rootfs. Runs *inside* the chroot as root.
#
# Injected above by sshd.py:  ALX_PORT ALX_USER ALX_PUBKEY
#
# Key-only, non-root, on a non-standard port. The reasoning:
#
# * The chroot shares Android's network namespace, so this listener is on the
#   device's real interfaces — Wi-Fi included. That is the point (reaching the
#   tablet from a workstation) but it means password auth would be a genuinely bad
#   idea, and the account has no password anyway.
# * Port 8022 rather than 22 because Android may already use low ports and because
#   22 on a phone's Wi-Fi interface attracts noise.
# * PermitRootLogin no: root is reachable via `sudo` from the user, so exposing it
#   over the network buys nothing.

set -eu

say() { printf '  %s\n' "$1"; }

command -v sshd >/dev/null 2>&1 || [ -x /usr/sbin/sshd ] || {
  echo "sshd not installed — apt-get install openssh-server" >&2; exit 1
}

# Host keys. Missing ones are generated; existing ones are left alone so the
# host's known_hosts entry stays valid across restarts.
ssh-keygen -A >/dev/null 2>&1
say "host keys present: $(ls /etc/ssh/ssh_host_*_key 2>/dev/null | wc -l)"

# sshd refuses to start without its privilege-separation directory, and /run is a
# fresh tmpfs on every `up`, so this cannot be a one-time step.
mkdir -p /run/sshd
chmod 0755 /run/sshd

cat > /etc/ssh/sshd_config.d/10-androlinux.conf <<EOF
# Written by androlinux.
Port $ALX_PORT
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers $ALX_USER
X11Forwarding yes
PrintMotd no
AcceptEnv LANG LC_*
EOF
say "config: port $ALX_PORT, key-only, user $ALX_USER"

# The caller's public key, so there is a way in at all given passwords are off.
if [ -n "${ALX_PUBKEY:-}" ]; then
  home=$(getent passwd "$ALX_USER" | cut -d: -f6)
  mkdir -p "$home/.ssh"
  chmod 700 "$home/.ssh"
  touch "$home/.ssh/authorized_keys"
  if ! grep -qF "$ALX_PUBKEY" "$home/.ssh/authorized_keys"; then
    printf '%s\n' "$ALX_PUBKEY" >> "$home/.ssh/authorized_keys"
    say "authorised key added for $ALX_USER"
  else
    say "authorised key already present"
  fi
  chmod 600 "$home/.ssh/authorized_keys"
  chown -R "$ALX_USER" "$home/.ssh"
fi

# A marker so `up` restarts sshd after a reboot without being told again.
touch /etc/androlinux-ssh

# Validate before starting, so a bad config fails here rather than silently.
/usr/sbin/sshd -t
say "config validated"

if pgrep -x sshd >/dev/null 2>&1; then
  say "sshd already running"
else
  # sshd daemonises itself; setsid and the redirects keep adb from waiting on it.
  setsid /usr/sbin/sshd </dev/null >/dev/null 2>&1 &
  sleep 2
  if pgrep -x sshd >/dev/null 2>&1; then
    say "sshd started"
  else
    echo "sshd did not stay up; try /usr/sbin/sshd -Ddd for the reason" >&2
    exit 1
  fi
fi
