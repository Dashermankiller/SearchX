import logging
import os
import threading

import psycopg2
import psycopg2.extras
import psycopg2.pool

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://searchx:searchx@localhost:5432/searchx",
)

# One pool shared by all threads in this process.
# max=100 comfortably covers workers*threads for any reasonable Gunicorn config.
_pool = psycopg2.pool.ThreadedConnectionPool(2, 100, DATABASE_URL)
_conn_local = threading.local()


class _Conn:
    """
    Thin wrapper so every caller can use the same .execute() / .commit() API
    that the SQLite version exposed, while internally using psycopg2.
    DictCursor makes rows accessible by both column name and integer index,
    matching sqlite3.Row behaviour.
    """

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        cur = self._raw.cursor(cursor_factory=psycopg2.extras.DictCursor)
        try:
            cur.execute(sql, params or ())
        except psycopg2.Error:
            # Roll back the aborted transaction so the connection stays usable
            try:
                self._raw.rollback()
            except Exception:
                pass
            raise
        return cur

    def executemany(self, sql, seq):
        cur = self._raw.cursor()
        cur.executemany(sql, seq)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    # Context-manager support (used by search.py: with get_connection() as conn:)
    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self._raw.rollback()
        else:
            self._raw.commit()
        return False

    @property
    def closed(self):
        return self._raw.closed != 0


def get_connection() -> _Conn:
    """Return this thread's borrowed pool connection, opening one if needed."""
    raw = getattr(_conn_local, "conn", None)
    if raw is None or raw.closed != 0:
        raw = _pool.getconn()
        _conn_local.conn = raw
    return _Conn(raw)


def close_connection():
    """Return the thread-local connection to the pool (called in post_fork hooks)."""
    raw = getattr(_conn_local, "conn", None)
    if raw:
        try:
            _pool.putconn(raw)
        except Exception:
            pass
    _conn_local.conn = None


def commit():
    """Commit the current thread's pending transaction."""
    raw = getattr(_conn_local, "conn", None)
    if raw:
        raw.commit()


def _to_dict(row) -> dict:
    """Convert a DictRow to a plain dict, serialising timestamps to ISO strings."""
    if row is None:
        return {}
    d = dict(row)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


# ── Schema ─────────────────────────────────────────────────────────────────

def init_db():
    conn = get_connection()

    # search_vector is a GENERATED STORED column — always in sync, no triggers needed.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id            BIGSERIAL    PRIMARY KEY,
            url           TEXT         UNIQUE NOT NULL,
            title         TEXT         NOT NULL DEFAULT '',
            content       TEXT         NOT NULL DEFAULT '',
            filetype      TEXT         NOT NULL DEFAULT '',
            media_type    TEXT         NOT NULL DEFAULT '',
            source        TEXT         NOT NULL DEFAULT '',
            file_path     TEXT,
            file_size     BIGINT,
            metadata      JSONB        NOT NULL DEFAULT '{}',
            indexed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            source_id     BIGINT,
            mtime         DOUBLE PRECISION,
            file_hash     TEXT,
            thumb_path    TEXT,
            search_vector TSVECTOR GENERATED ALWAYS AS (
                setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(left(content, 50000), '')), 'B')
            ) STORED
        )
    """)

    for stmt in [
        "CREATE INDEX IF NOT EXISTS idx_fts        ON documents USING GIN(search_vector)",
        "CREATE INDEX IF NOT EXISTS idx_filetype   ON documents(filetype)",
        "CREATE INDEX IF NOT EXISTS idx_media_type ON documents(media_type)",
        "CREATE INDEX IF NOT EXISTS idx_source     ON documents(source)",
        "CREATE INDEX IF NOT EXISTS idx_indexed_at ON documents(indexed_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_source_id  ON documents(source_id)",
        "CREATE INDEX IF NOT EXISTS idx_file_hash  ON documents(file_hash)",
        # Base-URL index: fast site: searches and domain grouping
        # Extracts the host from the URL, e.g. 'https://www.github.com/x' → 'github.com'
        """CREATE INDEX IF NOT EXISTS idx_base_url ON documents (
            LOWER(REGEXP_REPLACE(url, '^https?://(www\\.)?([^/?#]+).*$', '\\2', 'i'))
        )""",
    ]:
        conn.execute(stmt)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_sources (
            id           BIGSERIAL    PRIMARY KEY,
            type         TEXT         NOT NULL,
            path         TEXT         UNIQUE NOT NULL,
            label        TEXT,
            settings     JSONB        NOT NULL DEFAULT '{}',
            last_indexed TIMESTAMPTZ,
            doc_count    INTEGER      NOT NULL DEFAULT 0,
            enabled      BOOLEAN      NOT NULL DEFAULT TRUE
        )
    """)

    # ── Users ───────────────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            BIGSERIAL    PRIMARY KEY,
            username      TEXT         UNIQUE NOT NULL,
            password_hash TEXT         NOT NULL,
            role          TEXT         NOT NULL DEFAULT 'user',
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id  BIGINT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key      TEXT    NOT NULL,
            value    JSONB,
            PRIMARY KEY (user_id, key)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_history (
            id           BIGSERIAL    PRIMARY KEY,
            user_id      BIGINT       REFERENCES users(id) ON DELETE CASCADE,
            query        TEXT         NOT NULL,
            result_count INTEGER      NOT NULL DEFAULT 0,
            searched_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)
    # Migrate existing table: add user_id if it was created before auth was added
    conn.execute("""
        ALTER TABLE search_history
        ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES users(id) ON DELETE CASCADE
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_at ON search_history(searched_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_user ON search_history(user_id)"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS collections (
            id          BIGSERIAL    PRIMARY KEY,
            name        TEXT         NOT NULL,
            description TEXT         NOT NULL DEFAULT '',
            created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS collection_items (
            id            BIGSERIAL    PRIMARY KEY,
            collection_id BIGINT       NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            doc_id        BIGINT       NOT NULL REFERENCES documents(id)   ON DELETE CASCADE,
            added_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UNIQUE(collection_id, doc_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS crawl_queue (
            id          BIGSERIAL    PRIMARY KEY,
            url         TEXT         UNIQUE NOT NULL,
            domain      TEXT         NOT NULL DEFAULT '',
            depth       INTEGER      NOT NULL DEFAULT 0,
            status      TEXT         NOT NULL DEFAULT 'pending',
            added_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            crawled_at  TIMESTAMPTZ,
            error       TEXT
        )
    """)
    for stmt in [
        "CREATE INDEX IF NOT EXISTS idx_cq_status ON crawl_queue(status)",
        "CREATE INDEX IF NOT EXISTS idx_cq_domain ON crawl_queue(domain)",
        "CREATE INDEX IF NOT EXISTS idx_cq_crawled ON crawl_queue(crawled_at DESC NULLS LAST)",
    ]:
        conn.execute(stmt)

    conn.commit()


def init_db_extras():
    """Add columns and tables introduced after the initial schema (safe to run repeatedly)."""
    conn = get_connection()

    # SimHash for near-duplicate detection
    conn.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS simhash BIGINT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_simhash ON documents(simhash)")

    # PageRank score (0.0–1.0, normalised)
    conn.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS pagerank FLOAT DEFAULT 0.0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pagerank ON documents(pagerank DESC)")

    # Link graph for PageRank computation
    conn.execute("""
        CREATE TABLE IF NOT EXISTS link_graph (
            source_url  TEXT NOT NULL,
            target_url  TEXT NOT NULL,
            PRIMARY KEY (source_url, target_url)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lg_target ON link_graph(target_url)")

    # pg_trgm — fuzzy search and spelling correction
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trgm_title   ON documents USING GIN(title gin_trgm_ops)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trgm_history ON search_history USING GIN(query gin_trgm_ops)"
    )

    # Semantic search — vector embedding column (pgvector if available, else REAL[])
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding vector(384)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding ON documents "
            "USING hnsw (embedding vector_cosine_ops)"
        )
        log.info("pgvector enabled for semantic search")
    except Exception:
        # pgvector not installed — fall back to plain float array
        conn.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding REAL[]")
        log.info("pgvector unavailable — using REAL[] for embeddings (Python cosine fallback)")

    conn.commit()


def did_you_mean(query: str, result_count: int) -> str | None:
    """
    Return a spelling suggestion when the query returns few/no results.
    Checks past search queries first (users know what works), then document titles.
    Returns None if no good suggestion found or results are already good.
    """
    if result_count >= 5 or not query or len(query) < 3:
        return None
    conn = get_connection()

    # 1. Check search history for a similar query that likely had results
    row = conn.execute(
        """SELECT query FROM search_history
           WHERE similarity(query, %s) > 0.35
             AND query <> %s
             AND result_count > 0
           ORDER BY similarity(query, %s) DESC, result_count DESC
           LIMIT 1""",
        (query, query, query),
    ).fetchone()
    if row:
        return row["query"]

    # 2. Fall back to document titles
    row = conn.execute(
        """SELECT title FROM documents
           WHERE similarity(title, %s) > 0.4
             AND title <> %s
           ORDER BY similarity(title, %s) DESC
           LIMIT 1""",
        (query, query, query),
    ).fetchone()
    if row:
        # Return just the matching word(s), not a full title
        words = [w for w in row["title"].split() if len(w) > 3]
        if words:
            return " ".join(words[:4])

    return None


def is_near_duplicate(simhash_val: int, url: str, threshold: int = 3) -> bool:
    """Return True if an existing document is within *threshold* Hamming bits of *simhash_val*.

    Only compares against other webcrawler-sourced documents — local files and
    manually added web sources are always indexed regardless of similarity.
    Uses length(replace(...)) to count set bits — compatible with all PostgreSQL versions.
    """
    conn = get_connection()
    row = conn.execute("""
        SELECT id FROM documents
        WHERE  simhash IS NOT NULL
          AND  source = 'webcrawler'
          AND  url    != %s
          AND  length(replace(((simhash # %s)::bit(64))::text, '0', '')) <= %s
        LIMIT  1
    """, (url, simhash_val, threshold)).fetchone()
    return row is not None


# ── Index sources ──────────────────────────────────────────────────────────

def upsert_source(type_, path, label=None, settings=None) -> int:
    conn = get_connection()
    cur = conn.execute("""
        INSERT INTO index_sources (type, path, label, settings)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (path) DO UPDATE SET
            label    = COALESCE(EXCLUDED.label, index_sources.label),
            settings = EXCLUDED.settings
        RETURNING id
    """, (type_, path, label or path, psycopg2.extras.Json(settings or {})))
    row = cur.fetchone()
    conn.commit()
    return row[0]


def finish_source(source_id: int):
    conn = get_connection()
    count = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE source_id = %s", (source_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE index_sources SET last_indexed = NOW(), doc_count = %s WHERE id = %s",
        (count, source_id),
    )
    conn.commit()


def get_sources() -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM index_sources ORDER BY last_indexed DESC NULLS LAST"
    ).fetchall()
    result = []
    for r in rows:
        d = _to_dict(r)
        if not isinstance(d.get("settings"), dict):
            d["settings"] = {}
        result.append(d)
    return result


def get_source(source_id: int):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM index_sources WHERE id = %s", (source_id,)
    ).fetchone()
    if not row:
        return None
    d = _to_dict(row)
    if not isinstance(d.get("settings"), dict):
        d["settings"] = {}
    return d


def update_source_settings(source_id: int, label: str, settings: dict):
    conn = get_connection()
    conn.execute(
        "UPDATE index_sources SET label = %s, settings = %s WHERE id = %s",
        (label, psycopg2.extras.Json(settings), source_id),
    )
    conn.commit()


def delete_source(source_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM documents WHERE source_id = %s", (source_id,))
    conn.execute("DELETE FROM index_sources WHERE id = %s", (source_id,))
    conn.commit()
    rebuild_fts()


def toggle_source(source_id: int, enabled: bool):
    conn = get_connection()
    conn.execute(
        "UPDATE index_sources SET enabled = %s WHERE id = %s", (enabled, source_id)
    )
    conn.commit()


# ── Documents ──────────────────────────────────────────────────────────────

def get_document_mtime(url: str):
    conn = get_connection()
    row = conn.execute(
        "SELECT mtime FROM documents WHERE url = %s", (url,)
    ).fetchone()
    if row and row["mtime"] is not None:
        return float(row["mtime"])
    return None


def upsert_document(url, title, content, filetype, media_type, source,
                    file_path=None, file_size=None, metadata=None, source_id=None,
                    mtime=None, file_hash=None, thumb_path=None):
    from simhash_util import compute as _simhash

    sh = _simhash(content) if content else None

    # Skip webcrawler pages that are near-duplicates of already-indexed content
    if sh is not None and source == "webcrawler" and is_near_duplicate(sh, url):
        log.debug("Near-duplicate skipped: %s", url)
        return False

    # Generate embedding for semantic search (title + first 512 chars of content)
    emb = None
    try:
        from embeddings import embed as _embed
        text_for_embed = f"{title or ''} {(content or '')[:512]}".strip()
        if text_for_embed:
            emb = _embed(text_for_embed)
    except Exception:
        pass

    conn = get_connection()
    conn.execute("""
        INSERT INTO documents
            (url, title, content, filetype, media_type, source,
             file_path, file_size, metadata, indexed_at,
             source_id, mtime, file_hash, thumb_path, simhash, embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s)
        ON CONFLICT (url) DO UPDATE SET
            title      = EXCLUDED.title,
            content    = EXCLUDED.content,
            filetype   = EXCLUDED.filetype,
            media_type = EXCLUDED.media_type,
            file_size  = EXCLUDED.file_size,
            metadata   = EXCLUDED.metadata,
            indexed_at = EXCLUDED.indexed_at,
            source_id  = EXCLUDED.source_id,
            mtime      = EXCLUDED.mtime,
            file_hash  = EXCLUDED.file_hash,
            thumb_path = COALESCE(EXCLUDED.thumb_path, documents.thumb_path),
            simhash    = EXCLUDED.simhash,
            embedding  = COALESCE(EXCLUDED.embedding, documents.embedding)
    """, (
        url, title or "", content or "", filetype or "", media_type or "",
        source or "", file_path, file_size,
        psycopg2.extras.Json(metadata or {}),
        source_id, mtime, file_hash, thumb_path, sh, emb,
    ))
    return True


def get_stats():
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    by_type = conn.execute(
        "SELECT media_type, COUNT(*) AS cnt FROM documents GROUP BY media_type"
    ).fetchall()
    by_source = conn.execute(
        "SELECT source, COUNT(*) AS cnt FROM documents GROUP BY source"
    ).fetchall()
    return {
        "total": total,
        "by_type":   {r["media_type"]: r["cnt"] for r in by_type},
        "by_source": {r["source"]:     r["cnt"] for r in by_source},
    }


def rebuild_fts():
    """Invalidate cache and refresh query-planner stats.
    The search_vector column is always current (generated), so no rebuild is needed."""
    from cache import search_cache
    search_cache.invalidate()
    conn = get_connection()
    conn.execute("ANALYZE documents")
    conn.commit()


# ── Search History ─────────────────────────────────────────────────────────

def add_search_history(query: str, result_count: int = 0, user_id: int = None):
    conn = get_connection()
    recent = conn.execute(
        """SELECT id FROM search_history
           WHERE query = %s AND user_id IS NOT DISTINCT FROM %s
             AND searched_at > NOW() - INTERVAL '1 hour'
           LIMIT 1""",
        (query, user_id),
    ).fetchone()
    if not recent:
        conn.execute(
            "INSERT INTO search_history (query, result_count, user_id) VALUES (%s, %s, %s)",
            (query, result_count, user_id),
        )
        conn.execute("""
            DELETE FROM search_history WHERE id NOT IN (
                SELECT id FROM (
                    SELECT id FROM search_history
                    WHERE user_id IS NOT DISTINCT FROM %s
                    ORDER BY searched_at DESC LIMIT 500
                ) sub
            ) AND user_id IS NOT DISTINCT FROM %s
        """, (user_id, user_id))
        conn.commit()


def get_search_history(limit: int = 100, user_id: int = None) -> list:
    conn = get_connection()
    if user_id is not None:
        rows = conn.execute(
            "SELECT * FROM search_history WHERE user_id = %s ORDER BY searched_at DESC LIMIT %s",
            (user_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM search_history ORDER BY searched_at DESC LIMIT %s", (limit,)
        ).fetchall()
    return [_to_dict(r) for r in rows]


def get_recent_queries(limit: int = 8, user_id: int = None) -> list:
    conn = get_connection()
    if user_id is not None:
        rows = conn.execute(
            """SELECT query, MAX(searched_at) AS last
               FROM search_history WHERE user_id = %s
               GROUP BY query ORDER BY last DESC LIMIT %s""",
            (user_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT query, MAX(searched_at) AS last
               FROM search_history
               GROUP BY query ORDER BY last DESC LIMIT %s""",
            (limit,),
        ).fetchall()
    return [r["query"] for r in rows]


def clear_search_history(user_id: int = None):
    conn = get_connection()
    if user_id is not None:
        conn.execute("DELETE FROM search_history WHERE user_id = %s", (user_id,))
    else:
        conn.execute("DELETE FROM search_history")
    conn.commit()


# ── Users ───────────────────────────────────────────────────────────────────

def create_user(username: str, password_hash: str, role: str = "user") -> int | None:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
            (username.strip().lower(), password_hash, role),
        )
        uid = cur.fetchone()[0]
        conn.commit()
        return uid
    except psycopg2.errors.UniqueViolation:
        return None


def get_user_by_username(username: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM users WHERE username = %s", (username.strip().lower(),)
    ).fetchone()
    return _to_dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    return _to_dict(row) if row else None


def get_all_users() -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, username, role, created_at FROM users ORDER BY created_at"
    ).fetchall()
    return [_to_dict(r) for r in rows]


def update_password(user_id: int, new_hash: str):
    conn = get_connection()
    conn.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s", (new_hash, user_id)
    )
    conn.commit()


def delete_user(user_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
    conn.commit()


def seed_admin():
    """Create the default admin account if no users exist."""
    from auth import hash_password
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count == 0:
        h = hash_password("admin")
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES ('admin', %s, 'admin')",
            (h,),
        )
        conn.commit()
        log.info("Default admin account created (username=admin password=admin)")


# ── User settings ────────────────────────────────────────────────────────────

def get_user_setting(user_id: int, key: str, default=None):
    conn = get_connection()
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id = %s AND key = %s",
        (user_id, key),
    ).fetchone()
    if row is None:
        return default
    import json
    v = row[0]
    return v if v is not None else default


def set_user_setting(user_id: int, key: str, value):
    conn = get_connection()
    import json
    conn.execute(
        """INSERT INTO user_settings (user_id, key, value) VALUES (%s, %s, %s)
           ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value""",
        (user_id, key, json.dumps(value)),
    )
    conn.commit()


def get_all_user_settings(user_id: int) -> dict:
    conn = get_connection()
    rows = conn.execute(
        "SELECT key, value FROM user_settings WHERE user_id = %s", (user_id,)
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


# ── Collections ────────────────────────────────────────────────────────────

def create_collection(name: str, description: str = "") -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO collections (name, description) VALUES (%s, %s) RETURNING id",
        (name, description),
    )
    cid = cur.fetchone()[0]
    conn.commit()
    return cid


def get_collections() -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT c.*, COUNT(ci.id) AS item_count
        FROM collections c
        LEFT JOIN collection_items ci ON ci.collection_id = c.id
        GROUP BY c.id ORDER BY c.created_at DESC
    """).fetchall()
    return [_to_dict(r) for r in rows]


def get_collection(collection_id: int):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM collections WHERE id = %s", (collection_id,)
    ).fetchone()
    return _to_dict(row) if row else None


def get_collection_items(collection_id: int) -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT d.*, ci.added_at
        FROM collection_items ci
        JOIN documents d ON d.id = ci.doc_id
        WHERE ci.collection_id = %s
        ORDER BY ci.added_at DESC
    """, (collection_id,)).fetchall()
    return [_to_dict(r) for r in rows]


def add_to_collection(collection_id: int, doc_id: int):
    conn = get_connection()
    conn.execute(
        """INSERT INTO collection_items (collection_id, doc_id) VALUES (%s, %s)
           ON CONFLICT DO NOTHING""",
        (collection_id, doc_id),
    )
    conn.commit()


def remove_from_collection(collection_id: int, doc_id: int):
    conn = get_connection()
    conn.execute(
        "DELETE FROM collection_items WHERE collection_id = %s AND doc_id = %s",
        (collection_id, doc_id),
    )
    conn.commit()


def delete_collection(collection_id: int):
    conn = get_connection()
    # ON DELETE CASCADE handles collection_items automatically
    conn.execute("DELETE FROM collections WHERE id = %s", (collection_id,))
    conn.commit()


def rename_collection(collection_id: int, name: str, description: str = ""):
    conn = get_connection()
    conn.execute(
        "UPDATE collections SET name = %s, description = %s WHERE id = %s",
        (name, description, collection_id),
    )
    conn.commit()


# ── Duplicates ─────────────────────────────────────────────────────────────

def get_duplicates() -> list:
    conn = get_connection()
    hashes = conn.execute("""
        SELECT file_hash FROM documents
        WHERE file_hash IS NOT NULL AND file_hash != ''
        GROUP BY file_hash HAVING COUNT(*) > 1
    """).fetchall()
    groups = []
    for h in hashes:
        rows = conn.execute(
            "SELECT * FROM documents WHERE file_hash = %s ORDER BY indexed_at",
            (h["file_hash"],),
        ).fetchall()
        groups.append([_to_dict(r) for r in rows])
    return groups


# ── Web crawler queue ───────────────────────────────────────────────────────

def queue_urls(urls: list, depth: int = 0):
    """Insert URLs into the crawl queue; silently skip duplicates."""
    if not urls:
        return
    from urllib.parse import urlparse
    conn = get_connection()
    for url in urls:
        try:
            domain = urlparse(url).netloc or ""
            conn.execute("""
                INSERT INTO crawl_queue (url, domain, depth)
                VALUES (%s, %s, %s)
                ON CONFLICT (url) DO NOTHING
            """, (url, domain, depth))
        except Exception:
            pass
    conn.commit()


def get_next_queued() -> dict | None:
    """Atomically claim one pending URL with SKIP LOCKED (safe for concurrent workers)."""
    conn = get_connection()
    cur = conn.execute("""
        UPDATE crawl_queue
        SET    status = 'crawling'
        WHERE  id = (
            SELECT id FROM crawl_queue
            WHERE  status = 'pending'
            ORDER  BY depth ASC, added_at ASC
            LIMIT  1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, url, domain, depth
    """)
    row = cur.fetchone()
    conn.commit()
    return _to_dict(row) if row else None


def mark_crawled(url: str, success: bool = True, error: str = None):
    conn = get_connection()
    status = "crawled" if success else "failed"
    conn.execute("""
        UPDATE crawl_queue
        SET status = %s, crawled_at = NOW(), error = %s
        WHERE url = %s
    """, (status, error, url))
    conn.commit()


def get_queue_stats() -> dict:
    conn = get_connection()
    rows = conn.execute("""
        SELECT status, COUNT(*) AS cnt FROM crawl_queue GROUP BY status
    """).fetchall()
    stats = {"pending": 0, "crawling": 0, "crawled": 0, "failed": 0, "total": 0}
    for r in rows:
        stats[r["status"]] = r["cnt"]
        stats["total"] += r["cnt"]
    return stats


def get_recent_crawled(limit: int = 50) -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT url, domain, depth, status, crawled_at, error
        FROM   crawl_queue
        WHERE  status IN ('crawled', 'failed', 'crawling')
        ORDER  BY crawled_at DESC NULLS LAST
        LIMIT  %s
    """, (limit,)).fetchall()
    return [_to_dict(r) for r in rows]


def clear_queue():
    conn = get_connection()
    conn.execute("DELETE FROM crawl_queue")
    conn.commit()


def get_pending_sample(limit: int = 20) -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT url, domain, depth FROM crawl_queue
        WHERE status = 'pending'
        ORDER BY depth ASC, added_at ASC
        LIMIT %s
    """, (limit,)).fetchall()
    return [_to_dict(r) for r in rows]


# ── Link graph ──────────────────────────────────────────────────────────────

def store_links(source_url: str, target_urls: list):
    """Record outbound links discovered on *source_url* (used for PageRank)."""
    if not target_urls:
        return
    conn = get_connection()
    for tgt in target_urls:
        try:
            conn.execute("""
                INSERT INTO link_graph (source_url, target_url)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
            """, (source_url, tgt))
        except Exception:
            pass
    conn.commit()


# ── PageRank ────────────────────────────────────────────────────────────────

def compute_pagerank(damping: float = 0.85, iterations: int = 20) -> int:
    """
    Run PageRank using igraph (C core) — handles million-node graphs efficiently.
    Writes normalised scores (0–1) back into documents.pagerank.
    Returns the number of documents updated.
    """
    import igraph as ig

    conn = get_connection()

    # Only rank URLs we have indexed
    doc_rows = conn.execute("SELECT url FROM documents").fetchall()
    doc_urls = [r["url"] for r in doc_rows]
    if not doc_urls:
        return 0
    url_to_idx = {url: i for i, url in enumerate(doc_urls)}

    # Load edges restricted to indexed documents on both ends
    edge_rows = conn.execute(
        "SELECT source_url, target_url FROM link_graph"
    ).fetchall()

    edges = []
    for r in edge_rows:
        src_i = url_to_idx.get(r["source_url"])
        tgt_i = url_to_idx.get(r["target_url"])
        if src_i is not None and tgt_i is not None and src_i != tgt_i:
            edges.append((src_i, tgt_i))

    # Build directed graph and compute PageRank in C
    g  = ig.Graph(n=len(doc_urls), edges=edges, directed=True)
    pr = g.pagerank(damping=damping)

    # Normalise to 0–1
    max_pr = max(pr) if pr else 1.0
    if max_pr == 0:
        max_pr = 1.0

    # Write back in batches
    BATCH = 500
    for i in range(0, len(doc_urls), BATCH):
        for j in range(i, min(i + BATCH, len(doc_urls))):
            conn.execute(
                "UPDATE documents SET pagerank = %s WHERE url = %s",
                (pr[j] / max_pr, doc_urls[j]),
            )
        conn.commit()

    return len(doc_urls)


# ── Analytics ───────────────────────────────────────────────────────────────

def get_analytics() -> dict:
    conn = get_connection()

    total = conn.execute("SELECT COUNT(*) FROM search_history").fetchone()[0]
    unique = conn.execute("SELECT COUNT(DISTINCT query) FROM search_history").fetchone()[0]

    top_queries = conn.execute("""
        SELECT query, COUNT(*) AS cnt, AVG(result_count)::int AS avg_results
        FROM search_history
        GROUP BY query ORDER BY cnt DESC LIMIT 20
    """).fetchall()

    zero_results = conn.execute("""
        SELECT query, COUNT(*) AS cnt, MAX(searched_at) AS last_seen
        FROM search_history
        WHERE result_count = 0
        GROUP BY query ORDER BY cnt DESC LIMIT 20
    """).fetchall()

    daily = conn.execute("""
        SELECT DATE(searched_at) AS day, COUNT(*) AS cnt
        FROM search_history
        WHERE searched_at > NOW() - INTERVAL '30 days'
        GROUP BY day ORDER BY day
    """).fetchall()

    hourly = conn.execute("""
        SELECT EXTRACT(HOUR FROM searched_at)::int AS hour, COUNT(*) AS cnt
        FROM search_history
        WHERE searched_at > NOW() - INTERVAL '30 days'
        GROUP BY hour ORDER BY hour
    """).fetchall()

    top_users = conn.execute("""
        SELECT u.username, COUNT(*) AS cnt
        FROM search_history sh
        JOIN users u ON u.id = sh.user_id
        GROUP BY u.username ORDER BY cnt DESC LIMIT 10
    """).fetchall()

    return {
        "total":        total,
        "unique":       unique,
        "top_queries":  [dict(r) for r in top_queries],
        "zero_results": [dict(r) for r in zero_results],
        "daily":        [{"day": str(r["day"]), "cnt": r["cnt"]} for r in daily],
        "hourly":       [{"hour": r["hour"], "cnt": r["cnt"]} for r in hourly],
        "top_users":    [dict(r) for r in top_users],
    }


# ── Related searches ─────────────────────────────────────────────────────────

def get_related_searches(query: str, limit: int = 6) -> list[str]:
    """Return popular queries similar to the given one, excluding exact match."""
    if not query or len(query) < 2:
        return []
    conn = get_connection()
    rows = conn.execute("""
        SELECT query, COUNT(*) AS cnt,
               similarity(query, %s) AS sim
        FROM search_history
        WHERE query <> %s
          AND similarity(query, %s) > 0.15
        GROUP BY query
        ORDER BY sim DESC, cnt DESC
        LIMIT %s
    """, (query, query, query, limit)).fetchall()
    return [r["query"] for r in rows]


# ── Embed missing documents (background backfill) ────────────────────────────

def embed_missing(batch: int = 100) -> int:
    """Generate embeddings for documents that don't have one yet. Returns count updated."""
    conn  = get_connection()
    rows  = conn.execute(
        "SELECT id, title, content FROM documents WHERE embedding IS NULL LIMIT %s",
        (batch,),
    ).fetchall()
    if not rows:
        return 0

    from embeddings import embed as _embed
    updated = 0
    for row in rows:
        text = f"{row['title']} {(row['content'] or '')[:512]}".strip()
        emb  = _embed(text)
        if emb:
            conn.execute("UPDATE documents SET embedding = %s WHERE id = %s", (emb, row["id"]))
            updated += 1
    conn.commit()
    return updated
