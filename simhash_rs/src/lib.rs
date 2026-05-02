use pyo3::prelude::*;
use sha1::{Digest, Sha1};

/// Hash one shingle with SHA-1, returning bits 0-63 of the digest.
/// Bit ordering matches Python's int(sha1.hexdigest(), 16) so existing
/// stored simhashes remain valid after switching to this extension.
#[inline]
fn hash_shingle(s: &str) -> u64 {
    let mut h = Sha1::new();
    h.update(s.as_bytes());
    let result = h.finalize();
    // The Python big-int has bit 0 = LSB = last byte of SHA-1.
    // Bytes 12-19 (last 8 bytes) hold bits 0-63.
    // Interpreted as big-endian u64: byte[12]=MSB=bits63-56, byte[19]=LSB=bits7-0.
    u64::from_be_bytes(result[12..20].try_into().unwrap())
}

/// Compute a 64-bit SimHash fingerprint of *text*.
/// Returns a signed i64 matching PostgreSQL BIGINT.
#[pyfunction]
fn compute(text: &str) -> i64 {
    let tokens: Vec<&str> = text.split_whitespace().collect();
    if tokens.is_empty() {
        return 0;
    }

    // 3-word shingles (same as Python version)
    let mut v = [0i64; 64];

    if tokens.len() >= 3 {
        for i in 0..tokens.len() - 2 {
            let shingle = format!("{} {} {}", tokens[i], tokens[i + 1], tokens[i + 2]);
            let h = hash_shingle(&shingle);
            for bit in 0..64u32 {
                if (h >> bit) & 1 == 1 {
                    v[bit as usize] += 1;
                } else {
                    v[bit as usize] -= 1;
                }
            }
        }
    } else {
        for token in &tokens {
            let h = hash_shingle(token);
            for bit in 0..64u32 {
                if (h >> bit) & 1 == 1 {
                    v[bit as usize] += 1;
                } else {
                    v[bit as usize] -= 1;
                }
            }
        }
    }

    let mut fp: u64 = 0;
    for bit in 0..64usize {
        if v[bit] > 0 {
            fp |= 1u64 << bit;
        }
    }

    // Reinterpret as signed i64 (matches PostgreSQL BIGINT)
    fp as i64
}

/// Count differing bits between two signed 64-bit SimHash values.
#[pyfunction]
fn hamming(a: i64, b: i64) -> u32 {
    (a ^ b).unsigned_abs().count_ones()
}

#[pymodule]
fn simhash_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compute, m)?)?;
    m.add_function(wrap_pyfunction!(hamming, m)?)?;
    Ok(())
}
