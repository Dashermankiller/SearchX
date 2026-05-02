#!/usr/bin/env python3
"""
Migrate data from search.db (SQLite) → PostgreSQL.

Usage:
    export DATABASE_URL=postgresql://searchx:searchx@localhost:5432/searchx
    python migrate_to_pg.py

The script is idempotent: rows whose URL already exists in PostgreSQL are
skipped (ON CONFLICT DO NOTHING), so it's safe to re-run after a partial failure.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

SQLITE_PATH = Path(__file__).parent / "search.db"
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://searchx:searchx@localhost:5432/searchx",
)

BATCH = 200   # rows per INSERT batch


def _ts(val):
    """Return a PostgreSQL-compatible timestamp string, or None."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _clean(val):
    """Strip NUL bytes that PostgreSQL rejects in text columns."""
    if val is None:
        return None
    return str(val).replace("\x00", "")


def _json(val):
    """Parse a JSON string from SQLite; return a dict (or {} on failure)."""
    if isinstance(val, dict):
        return val
    try:
        return json.loads(val or "{}")
    except Exception:
        return {}


def migrate_documents(sqlite_conn, pg_cur):
    rows = sqlite_conn.execute("""
        SELECT id, url, title, content, filetype, media_type, source,
               file_path, file_size, metadata, indexed_at,
               source_id, mtime, file_hash, thumb_path
        FROM documents
        ORDER BY id
    """).fetchall()

    inserted = skipped = 0
    batch = []

    def flush():
        nonlocal inserted, skipped
        if not batch:
            return
        pg_cur.executemany("""
            INSERT INTO documents
                (url, title, content, filetype, media_type, source,
                 file_path, file_size, metadata, indexed_at,
                 source_id, mtime, file_hash, thumb_path)
            VALUES (%(url)s, %(title)s, %(content)s, %(filetype)s, %(media_type)s,
                    %(source)s, %(file_path)s, %(file_size)s, %(metadata)s,
                    %(indexed_at)s, %(source_id)s, %(mtime)s, %(file_hash)s,
                    %(thumb_path)s)
            ON CONFLICT (url) DO NOTHING
        """, batch)
        inserted += pg_cur.rowcount
        skipped  += len(batch) - pg_cur.rowcount
        batch.clear()

    for r in rows:
        batch.append({
            "url":        _clean(r[1]),
            "title":      _clean(r[2]) or "",
            "content":    _clean(r[3]) or "",
            "filetype":   _clean(r[4]) or "",
            "media_type": _clean(r[5]) or "",
            "source":     _clean(r[6]) or "",
            "file_path":  _clean(r[7]),
            "file_size":  r[8],
            "metadata":   psycopg2.extras.Json(_json(r[9])),
            "indexed_at": _ts(r[10]),
            "source_id":  r[11],
            "mtime":      float(r[12]) if r[12] is not None else None,
            "file_hash":  _clean(r[13]),
            "thumb_path": _clean(r[14]),
        })
        if len(batch) >= BATCH:
            flush()

    flush()
    return len(rows), inserted, skipped


def migrate_sources(sqlite_conn, pg_cur):
    rows = sqlite_conn.execute("""
        SELECT type, path, label, settings, last_indexed, doc_count, enabled
        FROM index_sources ORDER BY id
    """).fetchall()

    inserted = 0
    for r in rows:
        pg_cur.execute("""
            INSERT INTO index_sources (type, path, label, settings, last_indexed, doc_count, enabled)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (path) DO NOTHING
        """, (
            r[0], r[1], r[2],
            psycopg2.extras.Json(_json(r[3])),
            _ts(r[4]),
            r[5] or 0,
            bool(r[6]) if r[6] is not None else True,
        ))
        inserted += pg_cur.rowcount
    return len(rows), inserted


def migrate_history(sqlite_conn, pg_cur):
    rows = sqlite_conn.execute("""
        SELECT query, result_count, searched_at FROM search_history ORDER BY id
    """).fetchall()

    inserted = 0
    for r in rows:
        pg_cur.execute("""
            INSERT INTO search_history (query, result_count, searched_at)
            VALUES (%s, %s, %s)
        """, (r[0], r[1] or 0, _ts(r[2])))
        inserted += 1
    return len(rows), inserted


def migrate_collections(sqlite_conn, pg_cur):
    rows = sqlite_conn.execute("""
        SELECT id, name, description, created_at FROM collections ORDER BY id
    """).fetchall()

    id_map = {}   # old SQLite id → new PG id
    for r in rows:
        pg_cur.execute("""
            INSERT INTO collections (name, description, created_at)
            VALUES (%s, %s, %s)
            RETURNING id
        """, (r[1], r[2] or "", _ts(r[3])))
        new_id = pg_cur.fetchone()[0]
        id_map[r[0]] = new_id

    return len(rows), id_map


def migrate_collection_items(sqlite_conn, pg_cur, col_id_map, doc_url_to_id):
    rows = sqlite_conn.execute("""
        SELECT ci.collection_id, d.url, ci.added_at
        FROM collection_items ci
        JOIN documents d ON d.id = ci.doc_id
        ORDER BY ci.id
    """).fetchall()

    inserted = skipped = 0
    for r in rows:
        new_col_id = col_id_map.get(r[0])
        new_doc_id = doc_url_to_id.get(r[1])
        if new_col_id is None or new_doc_id is None:
            skipped += 1
            continue
        pg_cur.execute("""
            INSERT INTO collection_items (collection_id, doc_id, added_at)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (new_col_id, new_doc_id, _ts(r[2])))
        inserted += pg_cur.rowcount

    return len(rows), inserted, skipped


def main():
    if not SQLITE_PATH.exists():
        print(f"ERROR: SQLite database not found at {SQLITE_PATH}")
        sys.exit(1)

    print(f"Source : {SQLITE_PATH}")
    print(f"Target : {DATABASE_URL}\n")

    # ── Connect ─────────────────────────────────────────────────────────────
    try:
        pg = psycopg2.connect(DATABASE_URL)
        pg.autocommit = False
        pg_cur = pg.cursor(cursor_factory=psycopg2.extras.DictCursor)
    except Exception as e:
        print(f"ERROR connecting to PostgreSQL: {e}")
        print("\nMake sure you ran setup_db.py and that DATABASE_URL is set correctly.")
        sys.exit(1)

    sqlite_conn = sqlite3.connect(str(SQLITE_PATH))
    sqlite_conn.row_factory = sqlite3.Row

    # ── Init schema ─────────────────────────────────────────────────────────
    print("Initialising PostgreSQL schema…")
    sys.path.insert(0, str(Path(__file__).parent))
    import database as db
    db.init_db()
    print("  Schema ready.\n")

    # ── Sources ─────────────────────────────────────────────────────────────
    print("Migrating index_sources…")
    total, ins = migrate_sources(sqlite_conn, pg_cur)
    pg.commit()
    print(f"  {total} rows → {ins} inserted, {total-ins} already existed\n")

    # ── Documents ───────────────────────────────────────────────────────────
    print("Migrating documents…")
    total, ins, skipped = migrate_documents(sqlite_conn, pg_cur)
    pg.commit()
    print(f"  {total} rows → {ins} inserted, {skipped} already existed\n")

    # ── History ─────────────────────────────────────────────────────────────
    print("Migrating search_history…")
    total, ins = migrate_history(sqlite_conn, pg_cur)
    pg.commit()
    print(f"  {total} rows → {ins} inserted\n")

    # ── Collections + items ─────────────────────────────────────────────────
    print("Migrating collections…")
    total, col_id_map = migrate_collections(sqlite_conn, pg_cur)
    pg.commit()
    print(f"  {total} collections migrated\n")

    if col_id_map:
        # Build URL→PG-id map for collection items
        pg_cur.execute("SELECT id, url FROM documents")
        doc_url_to_id = {r["url"]: r["id"] for r in pg_cur.fetchall()}

        print("Migrating collection_items…")
        total, ins, skipped = migrate_collection_items(
            sqlite_conn, pg_cur, col_id_map, doc_url_to_id
        )
        pg.commit()
        print(f"  {total} rows → {ins} inserted, {skipped} skipped\n")

    # ── Final stats ─────────────────────────────────────────────────────────
    pg_cur.execute("SELECT COUNT(*) FROM documents")
    doc_count = pg_cur.fetchone()[0]
    pg_cur.execute("SELECT COUNT(*) FROM index_sources")
    src_count = pg_cur.fetchone()[0]

    print("Migration complete!")
    print(f"  Documents in PostgreSQL : {doc_count}")
    print(f"  Sources   in PostgreSQL : {src_count}")

    pg_cur.close()
    pg.close()
    sqlite_conn.close()


if __name__ == "__main__":
    main()
