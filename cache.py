import time
import threading


class TTLCache:
    """
    Thread-safe in-process TTL cache.
    Each Gunicorn worker has its own instance — no IPC needed for reads.
    Invalidated on FTS rebuild so stale results are never served.
    """

    def __init__(self, ttl: int = 180, maxsize: int = 1000):
        self._store: dict = {}
        self._ttl = ttl
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            val, exp = entry
            if time.monotonic() > exp:
                del self._store[key]
                self._misses += 1
                return None
            self._hits += 1
            return val

    def set(self, key: str, val):
        with self._lock:
            if len(self._store) >= self._maxsize:
                # Evict the entry closest to expiry
                oldest = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest]
            self._store[key] = (val, time.monotonic() + self._ttl)

    def invalidate(self):
        with self._lock:
            self._store.clear()

    @property
    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size":     len(self._store),
                "maxsize":  self._maxsize,
                "hits":     self._hits,
                "misses":   self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
            }


# 3-minute TTL, up to 1000 unique query+page+sort combos per worker
search_cache = TTLCache(ttl=180, maxsize=1000)

# 10-minute TTL for AI answers (slower to generate, longer to keep)
answer_cache = TTLCache(ttl=600, maxsize=500)
