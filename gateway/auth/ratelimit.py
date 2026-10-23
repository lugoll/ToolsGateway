"""Login throttling, in memory (one process): per client address and per user name.

After FREE failures inside WINDOW, each further attempt has to wait twice as
long as the previous one (1 s, 2 s, 4 s, ... capped at MAX_WAIT). A success
clears the counter for that name and address.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

FREE = 5
WINDOW = 15 * 60.0
MAX_WAIT = 15 * 60.0


@dataclass
class _Bucket:
    failures: list[float] = field(default_factory=list)
    blocked_until: float = 0.0


class LoginLimiter:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def _keys(self, address: str, name: str) -> list[str]:
        return [f"ip:{address}", f"user:{name.lower()}"]

    def wait(self, address: str, name: str) -> float:
        """Seconds until the next attempt is allowed (0 = now)."""
        now = self.clock()
        return max([0.0] + [b.blocked_until - now for k in self._keys(address, name)
                            if (b := self._buckets.get(k))])

    def failed(self, address: str, name: str) -> None:
        now = self.clock()
        for key in self._keys(address, name):
            b = self._buckets.setdefault(key, _Bucket())
            b.failures = [t for t in b.failures if now - t < WINDOW] + [now]
            extra = len(b.failures) - FREE
            if extra > 0:
                b.blocked_until = now + min(MAX_WAIT, 2.0 ** (extra - 1))
        if len(self._buckets) > 10_000:                       # forget stale entries
            self._buckets = {k: b for k, b in self._buckets.items() if b.failures and now - b.failures[-1] < WINDOW}

    def succeeded(self, address: str, name: str) -> None:
        for key in self._keys(address, name):
            self._buckets.pop(key, None)
