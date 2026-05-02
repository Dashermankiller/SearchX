"""
SimHash — locality-sensitive hashing for near-duplicate detection.

Uses the Rust extension (simhash_rs) when available — ~30x faster than
pure Python. Falls back to pure Python automatically if the extension
hasn't been built yet.
"""

try:
    from simhash_rs import compute, hamming   # Rust extension (maturin build)
    _BACKEND = "rust"
except ImportError:
    import hashlib

    def compute(text: str, bits: int = 64) -> int:
        """Pure-Python SimHash fallback — same algorithm as the Rust version."""
        tokens = text.lower().split()
        if not tokens:
            return 0
        shingles = (
            [f"{tokens[i]} {tokens[i+1]} {tokens[i+2]}" for i in range(len(tokens) - 2)]
            if len(tokens) >= 3
            else tokens
        )
        v = [0] * bits
        for s in shingles:
            h = int(hashlib.sha1(s.encode(), usedforsecurity=False).hexdigest(), 16)
            for i in range(bits):
                v[i] += 1 if (h >> i) & 1 else -1
        fp = sum(1 << i for i in range(bits) if v[i] > 0)
        if fp >= (1 << 63):
            fp -= (1 << 64)
        return fp

    def hamming(a: int, b: int) -> int:
        xor = (a ^ b) & 0xFFFFFFFFFFFFFFFF
        return bin(xor).count("1")

    _BACKEND = "python"
