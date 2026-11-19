"""Linux ingress-only syscall policy for public honeynet listeners."""

import ctypes as c
import errno
import runpy
import socket
import sys


class Compare(c.Structure):
    _fields_ = [("arg", c.c_uint), ("op", c.c_uint), ("datum_a", c.c_uint64), ("datum_b", c.c_uint64)]


ALLOW = 0x7FFF0000
DENY = 0x00050000 | errno.EPERM
NE = 1
LT = 2
EQ = 4
GT = 6
MASKED_EQ = 7


def install():
    if sys.platform != "linux":
        raise RuntimeError("Ingress-only deployment requires Linux")
    lib = c.CDLL("libseccomp.so.2")
    lib.seccomp_init.argtypes = [c.c_uint32]
    lib.seccomp_init.restype = c.c_void_p
    lib.seccomp_release.argtypes = [c.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [c.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = c.c_int
    lib.seccomp_rule_add_array.argtypes = [c.c_void_p, c.c_uint32, c.c_int, c.c_uint, c.POINTER(Compare)]
    lib.seccomp_load.argtypes = [c.c_void_p]
    policy = lib.seccomp_init(ALLOW)
    if not policy:
        raise RuntimeError("Cannot initialize ingress-only policy")
    try:
        def deny(name, *comparisons):
            number = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                raise RuntimeError(f"Unrecognized Linux syscall {name}")
            result = lib.seccomp_rule_add_array(policy, DENY, number, len(comparisons),
                                                (Compare * len(comparisons))(*comparisons))
            if result != 0:
                raise OSError(-result, f"Cannot install rule for {name}")

        for name in (
            "connect", "sendmsg", "sendmmsg", "io_uring_setup", "io_uring_enter", "io_uring_register",
            "ptrace", "pidfd_getfd", "process_vm_writev", "setns", "unshare",
        ):
            deny(name)
        # A TCP response from an accepted connection needs no destination.
        deny("sendto", Compare(4, NE, 0, 0))
        for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
            # Rules with masked equality prevent packet/UDP sockets while
            # preserving TCP listeners and local event-loop socket pairs.
            for kind in (socket.SOCK_DGRAM, socket.SOCK_RAW, socket.SOCK_RDM, socket.SOCK_SEQPACKET):
                deny("socket", Compare(0, EQ, family, 0), Compare(1, MASKED_EQ, 0xF, kind))
        # seccomp allows only one comparison for an argument in a rule. Linux
        # family numbers are contiguous between these permitted families.
        deny("socketpair", Compare(0, NE, int(socket.AF_UNIX), 0))
        deny("socket", Compare(0, LT, int(socket.AF_UNIX), 0))
        deny("socket", Compare(0, GT, int(socket.AF_INET6), 0))
        for family in range(int(socket.AF_INET) + 1, int(socket.AF_INET6)):
            deny("socket", Compare(0, EQ, family, 0))
        result = lib.seccomp_load(policy)
        if result != 0:
            raise OSError(-result, "Cannot load ingress-only policy")
    finally:
        lib.seccomp_release(policy)


if __name__ == "__main__":
    install()
    sys.argv = sys.argv[1:]
    if sys.argv[0].endswith(".py"):
        runpy.run_path(sys.argv[0], run_name="__main__")
    else:
        runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)
