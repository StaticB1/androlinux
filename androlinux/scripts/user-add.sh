#!/bin/bash
# Create a normal user inside the rootfs. Runs *inside* the chroot as root.
#
# Injected above by users.py:  ALX_USER ALX_UID ALX_SHELL ALX_SUDO
#
# The interesting part is the supplementary groups. Android does not decide
# network access by capability, it decides by group membership: a process must be
# in AID_INET (gid 3003) to create a socket at all. A chroot process inherits no
# Android groups, so a freshly created distro user has *no network whatsoever* —
# not "slow DNS", nothing. Verified on an SM-T970:
#
#   setpriv --reuid=1000 --regid=1000 --clear-groups getent hosts deb.debian.org  -> FAILS
#   setpriv --reuid=1000 --regid=1000 --groups=3003  getent hosts deb.debian.org  -> RESOLVES
#
# This is the same mechanism that made apt fail with "Temporary failure resolving"
# while resolution worked perfectly as root: apt drops to its own _apt user.
#
# So the user is put into groups whose gids match Android's AIDs. The names are
# arbitrary and only for legibility; the numbers are what the kernel checks.

set -eu

say() { printf '  %s\n' "$1"; }

# gid    name              what it grants
#  3003  aid_inet          create sockets — without this there is no network
#  3004  aid_net_raw       raw sockets, so ping works
#  3005  aid_net_admin     network configuration
#  1015  aid_sdcard_rw     shared storage
#  1023  aid_media_rw      media directories
#  9997  aid_everybody     the catch-all Android group
ANDROID_GROUPS="3003:aid_inet 3004:aid_net_raw 3005:aid_net_admin 1015:aid_sdcard_rw 1023:aid_media_rw 9997:aid_everybody"

for pair in $ANDROID_GROUPS; do
  gid=${pair%%:*}
  name=${pair##*:}
  if ! getent group "$gid" >/dev/null 2>&1; then
    groupadd -g "$gid" "$name" 2>/dev/null || true
  fi
done
say "android groups present: $(echo "$ANDROID_GROUPS" | tr ' ' '\n' | cut -d: -f2 | tr '\n' ' ')"

# Resolve each gid to whatever name actually holds it — a distro may already use
# one of these numbers under a different name, and usermod wants names.
GROUP_NAMES=""
for pair in $ANDROID_GROUPS; do
  gid=${pair%%:*}
  n=$(getent group "$gid" 2>/dev/null | cut -d: -f1)
  [ -n "$n" ] && GROUP_NAMES="$GROUP_NAMES,$n"
done
GROUP_NAMES=${GROUP_NAMES#,}

# Distro container images park a placeholder account on uid 1000 — "ubuntu" on
# Ubuntu's, for instance. Matching the host's uid is worth keeping: file ownership
# then lines up if files ever move between the two. So take the uid over by
# renaming the placeholder rather than shunting the real user to 1001.
WANT_UID="$ALX_UID"
if ! id "$ALX_USER" >/dev/null 2>&1; then
  OCCUPANT=$(getent passwd "$WANT_UID" 2>/dev/null | cut -d: -f1 || true)
  if [ -n "${OCCUPANT:-}" ]; then
    case "$OCCUPANT" in
      ubuntu|debian|user|alpine|admin)
        usermod -l "$ALX_USER" -d "/home/$ALX_USER" -m "$OCCUPANT"
        groupmod -n "$ALX_USER" "$OCCUPANT" 2>/dev/null || true
        say "renamed the image's placeholder '$OCCUPANT' to $ALX_USER, keeping uid $WANT_UID"
        ;;
      *)
        WANT_UID=$(( $(getent passwd | cut -d: -f3 | sort -n | awk '$1>=1000 && $1<60000' | tail -1) + 1 ))
        say "uid $ALX_UID belongs to '$OCCUPANT' — not touching it, using $WANT_UID instead"
        ;;
    esac
  fi
fi

if id "$ALX_USER" >/dev/null 2>&1; then
  say "user $ALX_USER exists (uid $(id -u "$ALX_USER")) — updating its groups"
  usermod --shell "$ALX_SHELL" "$ALX_USER" 2>/dev/null || true
else
  useradd --create-home --uid "$WANT_UID" --shell "$ALX_SHELL" "$ALX_USER"
  say "created $ALX_USER (uid $WANT_UID, shell $ALX_SHELL)"
fi

usermod -aG "$GROUP_NAMES" "$ALX_USER"
say "network + storage groups: $GROUP_NAMES"

if [ "${ALX_SUDO:-1}" = "1" ]; then
  getent group sudo >/dev/null 2>&1 && usermod -aG sudo "$ALX_USER"
  # Passwordless, because this user has no password set at all: the account is
  # reached by descending from root through the chroot, never by logging in. A
  # password would be one more thing to lose, protecting nothing.
  mkdir -p /etc/sudoers.d
  printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$ALX_USER" > "/etc/sudoers.d/90-$ALX_USER"
  chmod 440 "/etc/sudoers.d/90-$ALX_USER"
  say "sudo: passwordless (the account has no password to begin with)"
fi

# The account is locked for *login* while still usable via su from root. Nothing
# should be able to authenticate as it over ssh or a console with an empty
# password.
passwd -l "$ALX_USER" >/dev/null 2>&1 || true

say "home: $(getent passwd "$ALX_USER" | cut -d: -f6)"
say "id: $(id "$ALX_USER")"

# Prove the thing that motivated all of this.
if su - "$ALX_USER" -c 'getent hosts deb.debian.org >/dev/null 2>&1'; then
  say "network as $ALX_USER: works"
else
  say "network as $ALX_USER: FAILED — check that gid 3003 is in the group list above"
  exit 1
fi
