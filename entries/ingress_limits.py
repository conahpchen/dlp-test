"""Optional limits confined to owned deception listeners."""

import ipaddress
import os
from time import monotonic


WINDOW = 10


def configured_limit(name):
    value = int(os.environ.get(name, "0"))
    if not 0 <= value <= 10000:
        raise ValueError(f"Set {name} to an integer from 0 through 10000")
    return value


def socket_peer(peer):
    try:
        return ipaddress.ip_address(peer[0]) if peer else None
    except (IndexError, TypeError, ValueError):
        return None


class BurstBudget:
    def __init__(self, count):
        self.count = count
        self.window = -1
        self.counts = {}

    def check(self, key):
        """Return whether to reject and whether this is the first rejection."""
        if not self.count or key is None:
            return False, False
        window = int(monotonic() // WINDOW)
        if window != self.window:
            self.window = window
            self.counts.clear()
        if key not in self.counts and len(self.counts) >= 4096:
            return True, False
        used = min(self.counts.get(key, 0) + 1, self.count + 2)
        self.counts[key] = used
        return used > self.count, used == self.count + 1
