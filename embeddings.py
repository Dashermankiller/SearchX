"""
Lightweight embedding module for semantic search.
Uses sentence-transformers all-MiniLM-L6-v2 (384 dims, ~90 MB, CPU-friendly).
Model downloads automatically from HuggingFace on first use.
"""
import logging
import numpy as np

log = logging.getLogger(__name__)

_model    = None
DIMS      = 384
_MODEL_ID = "all-MiniLM-L6-v2"


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        log.info("Loading embedding model %s (first use — downloading if needed)", _MODEL_ID)
        _model = SentenceTransformer(_MODEL_ID)
        log.info("Embedding model ready")
    return _model


def embed(text: str) -> list[float] | None:
    """Return a normalized 384-dim embedding vector, or None on failure."""
    if not text or not text.strip():
        return None
    try:
        model = _get_model()
        vec   = model.encode(text[:1024], normalize_embeddings=True, show_progress_bar=False)
        return vec.tolist()
    except Exception as e:
        log.debug("embed() error: %s", e)
        return None


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two already-normalised vectors (dot product)."""
    return float(np.dot(np.array(a, dtype=np.float32),
                        np.array(b, dtype=np.float32)))


def rerank(query_vec: list[float], rows: list[dict],
           alpha: float = 0.4) -> list[dict]:
    """
    Blend FTS rank (_rank) with semantic similarity.
    alpha controls semantic weight (0 = FTS only, 1 = semantic only).
    """
    if not query_vec or not rows:
        return rows
    for r in rows:
        emb = r.get("embedding")
        if emb:
            sim = cosine(query_vec, emb)
        else:
            sim = 0.0
        fts = float(r.get("_rank") or 0.0)
        r["_combined"] = (1 - alpha) * fts + alpha * sim
    return sorted(rows, key=lambda r: r["_combined"], reverse=True)
