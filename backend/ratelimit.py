"""Request rate limits and the bounded work queue (standard library only).

RateLimiter: a token bucket per key (session, login name or connection source). A bucket holds up to `burst`
requests (default: one minute's worth) and refills at per_minute / 60 per second; per_minute <= 0 turns it off.
WorkQueue: one request at a time on the GPU/CPU, at most max_waiting more waiting; the next one is refused at once
instead of tying up a server thread for the whole queue timeout.
"""
import math
import os
import threading
import time


def env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return int(default)


class RateLimiter:
    MAX_KEYS = 10000

    def __init__(self, per_minute, burst=None):
        self.per_minute = per_minute
        self.rate = per_minute / 60.0
        self.capacity = float(burst or per_minute)
        self.lock = threading.Lock()
        self.buckets = {}   # key -> [tokens, last refill time]
        self.reported = {}  # key -> last time a refusal was audited

    @property
    def enabled(self):
        return self.per_minute > 0

    def take(self, key, now=None):
        """Use one request of key's budget: 0 when allowed, else the seconds to wait (for Retry-After)."""
        if not self.enabled:
            return 0
        now = time.monotonic() if now is None else now
        with self.lock:
            tokens, last = self.buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self.buckets[key] = [tokens - 1, now]
                self._prune(now)
                return 0
            self.buckets[key] = [tokens, now]
            return max(1, math.ceil((1 - tokens) / self.rate))

    def give_back(self, key):
        """Refund one request (a successful login does not count against the source)."""
        with self.lock:
            if key in self.buckets:
                self.buckets[key][0] = min(self.capacity, self.buckets[key][0] + 1)

    def reset(self, key):
        with self.lock:
            self.buckets.pop(key, None)

    def should_report(self, key, every=60, now=None):
        """True at most once per `every` seconds per key, so a flood writes one audit line a minute."""
        now = time.monotonic() if now is None else now
        with self.lock:
            if now - self.reported.get(key, -every) < every:
                return False
            self.reported[key] = now
            return True

    def _prune(self, now):
        if len(self.buckets) <= self.MAX_KEYS:
            return
        full = [k for k, (t, last) in self.buckets.items() if t + (now - last) * self.rate >= self.capacity]
        for k in full:
            del self.buckets[k]
        self.reported = {k: v for k, v in self.reported.items() if now - v < 60}


class WorkQueue:
    def __init__(self, max_waiting):
        self.max_waiting = max_waiting
        self.slot = threading.BoundedSemaphore(1)
        self.lock = threading.Lock()
        self.waiting = 0

    def acquire(self, timeout):
        """'ok' (the caller must release), 'full' (too many waiting) or 'timeout'."""
        if self.slot.acquire(blocking=False):
            return 'ok'
        with self.lock:
            if self.waiting >= self.max_waiting:
                return 'full'
            self.waiting += 1
        try:
            return 'ok' if self.slot.acquire(timeout=timeout) else 'timeout'
        finally:
            with self.lock:
                self.waiting -= 1

    def release(self):
        self.slot.release()
