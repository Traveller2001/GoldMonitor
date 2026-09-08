"""Bounded, source-isolated observations using a monotonic clock."""

import math
import time
from collections import deque


class PriceHistory:
    # Settings support up to 120 minutes plus a reference-sample tolerance.
    RETENTION_SECONDS = 120 * 60 + 600

    def __init__(self):
        self._sources = {}
        self._quote_timestamps = {}

    def is_outdated(self, source, quote_timestamp):
        previous = self._quote_timestamps.get(source)
        return quote_timestamp is not None and previous is not None and quote_timestamp < previous

    def add(self, source, price, quote_timestamp=None, now=None):
        now = time.monotonic() if now is None else now
        if not math.isfinite(price) or price <= 0:
            return False
        self._prune(now)
        entries = self._sources.setdefault(source, deque(maxlen=20000))
        if entries and now <= entries[-1][0]:
            return False
        previous_quote = self._quote_timestamps.get(source)
        if quote_timestamp is not None:
            if previous_quote is not None and quote_timestamp < previous_quote:
                return False
            # A composite quote can change while its older FX timestamp stays fixed.
            if previous_quote == quote_timestamp and entries and entries[-1][1] == price:
                return False
            self._quote_timestamps[source] = quote_timestamp
        entries.append((now, price))
        return True

    def _prune(self, now):
        cutoff = now - self.RETENTION_SECONDS
        for entries in self._sources.values():
            while entries and entries[0][0] < cutoff:
                entries.popleft()

    def window(self, source, seconds, now=None):
        now = time.monotonic() if now is None else now
        self._prune(now)
        return [(ts, price) for ts, price in self._sources.get(source, ())
                if now - seconds <= ts <= now]

    def interval_change(self, source, price, seconds, refresh_seconds, now=None):
        now = time.monotonic() if now is None else now
        self._prune(now)
        cutoff = now - seconds
        # Coarse sampling must not relabel a much longer move as a short interval.
        tolerance = min(max(60, refresh_seconds * 2), seconds * 0.2)
        for ts, reference in reversed(self._sources.get(source, ())):
            if ts <= cutoff:
                if cutoff - ts <= tolerance:
                    return (price - reference) / reference * 100
                break
        return None
