"""androlinux — a full Linux userspace on an Android kernel, without virtualisation.

The Android kernel is already Linux. androlinux does not emulate or virtualise it:
it unpacks a real distro rootfs onto the device and runs that distro's userspace
directly on the running kernel, isolated only by namespaces (the same primitives
containers use). Native syscalls, native speed, real root, real systemd.
"""

__version__ = "0.1.0"
