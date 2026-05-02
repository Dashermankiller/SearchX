import csv
import io
import json
import os
import re
import platform
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, abort, send_file, Response)

sys.path.insert(0, str(Path(__file__).parent))

from database import (
    DATABASE_URL,
    init_db, init_db_extras, get_stats, get_connection, close_connection,
    get_sources, get_source, update_source_settings,
    delete_source, toggle_source, upsert_source,
    add_search_history, get_search_history, get_recent_queries, clear_search_history,
    create_collection, get_collections, get_collection,
    get_collection_items, add_to_collection, remove_from_collection,
    delete_collection, rename_collection,
    get_duplicates,
    seed_admin,
    create_user, get_user_by_username, get_user_by_id, get_all_users,
    update_password, delete_user,
    get_user_setting, set_user_setting, get_all_user_settings,
    get_analytics, get_related_searches, embed_missing,
)
from search import search, suggestions, get_answer, stream_answer
from indexer import run_index_job, reindex_source, get_index_status, index_single_url
import config as cfg_module
from security import validate_indexed_path, is_safe_url
from crawler import start_crawler, stop_crawler, get_crawler_status
from auth import (
    hash_password, verify_password,
    set_auth_cookie, clear_auth_cookie, load_user,
    login_required, admin_required, optional_user,
    current_user,
)

app = Flask(__name__)
app.jinja_env.globals["enumerate"] = enumerate
app.jinja_env.filters["regex_search"] = lambda s, pat: bool(re.search(pat, s))
init_db()
init_db_extras()
seed_admin()   # create default admin account on first run


# ── Inject current_user into every template ──────────────────────────────────

@app.context_processor
def _inject_user():
    from flask import g
    user_payload = load_user()
    user = None
    if user_payload:
        user = get_user_by_id(user_payload["id"])
    return {"current_user": user}


# ── Auto web-crawl scheduler ────────────────────────────────────────────────

def _start_auto_crawl():
    def _loop():
        while True:
            time.sleep(60)   # wake up every minute to check
            try:
                cfg = cfg_module.load()
                if not cfg.get("auto_crawl_enabled", False):
                    continue
                interval_sec = cfg.get("auto_crawl_interval_hours", 24) * 3600
                last         = cfg.get("auto_crawl_last", 0)
                if time.time() - last < interval_sec:
                    continue
                # It's time — crawl all enabled web sources
                web_sources = [
                    s for s in get_sources()
                    if s["type"] == "web" and s.get("enabled", True)
                ]
                if not web_sources:
                    continue
                app.logger.info("Auto-crawl: starting on %d source(s)", len(web_sources))
                run_index_job([], [s["path"] for s in web_sources], {
                    "depth":       2,
                    "max_pages":   200,
                    "same_domain": True,
                    "delay":       0.5,
                })
                cfg = cfg_module.load()   # reload — user may have changed settings mid-run
                cfg["auto_crawl_last"] = time.time()
                cfg_module.save(cfg)
                app.logger.info("Auto-crawl: finished")
            except Exception as exc:
                app.logger.error("Auto-crawl error: %s", exc)

    t = threading.Thread(target=_loop, daemon=True, name="auto-crawl")
    t.start()


_start_auto_crawl()


# ── PageRank background scheduler ───────────────────────────────────────────

def _start_pagerank_scheduler():
    def _loop():
        time.sleep(300)   # wait 5 min after startup before first run
        while True:
            try:
                from database import compute_pagerank
                n = compute_pagerank()
                if n:
                    app.logger.info("PageRank computed for %d documents", n)
            except Exception as exc:
                app.logger.error("PageRank scheduler error: %s", exc)
            time.sleep(3600)  # recompute every hour

    threading.Thread(target=_loop, daemon=True, name="pagerank").start()


_start_pagerank_scheduler()


# Return the thread-local DB connection to the pool after every request.
@app.teardown_appcontext
def teardown_db(_exc):
    close_connection()


# ── Security headers ────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"]  = "nosniff"
    response.headers["X-Frame-Options"]          = "SAMEORIGIN"
    response.headers["Referrer-Policy"]          = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"]  = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.plyr.io; "
        "style-src 'self' 'unsafe-inline' https://cdn.plyr.io; "
        "img-src 'self' data: https: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self';"
    )
    return response


# ── Error handlers ──────────────────────────────────────────────────────────

@app.errorhandler(403)
def err_403(e):
    return render_template("error.html", code=403,
                           title="Access Denied",
                           message="You don't have permission to access this resource."), 403


@app.errorhandler(404)
def err_404(e):
    return render_template("error.html", code=404,
                           title="Not Found",
                           message="The page you're looking for doesn't exist."), 404


@app.errorhandler(429)
def err_429(e):
    return render_template("error.html", code=429,
                           title="Too Many Requests",
                           message="Slow down — you're sending too many requests."), 429


@app.errorhandler(500)
def err_500(e):
    app.logger.error("Internal error: %s", e)
    return render_template("error.html", code=500,
                           title="Server Error",
                           message="Something went wrong on our end. Try again in a moment."), 500



# ── Homepage ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    stats = get_stats()
    recent = get_recent_queries(limit=8)
    return render_template("index.html", stats=stats, recent=recent)


# ── Search ──────────────────────────────────────────────────────────────────

@app.route("/search")
def search_page():
    query = request.args.get("q", "").strip()
    if not query:
        return redirect(url_for("index"))

    page = max(1, int(request.args.get("page", 1)))
    per_page = int(request.args.get("per_page", 10))
    per_page = min(max(per_page, 5), 50)
    sort = request.args.get("sort", "date_desc")

    results = search(query, page=page, per_page=per_page, sort=sort)

    if page == 1:
        u = load_user()
        add_search_history(query, results["total"], user_id=u["id"] if u else None)

    related = []
    try:
        related = get_related_searches(query, limit=6)
    except Exception:
        pass

    stats = get_stats()
    collections = get_collections()
    return render_template("results.html", results=results, stats=stats,
                           query=query, sort=sort, collections=collections,
                           related=related)


@app.route("/api/suggest")
def api_suggest():
    q = request.args.get("q", "")
    return jsonify(suggestions(q))


@app.route("/api/tts")
def api_tts():
    text = request.args.get("text", "").strip()[:2000]
    if not text:
        abort(400)
    c        = cfg_module.load()
    provider = c.get("tts_provider", "edge")

    if provider == "elevenlabs":
        import requests as _req
        key = c.get("elevenlabs_api_key", "").strip()
        if not key:
            abort(503, "No ElevenLabs API key")
        voice_id = c.get("elevenlabs_voice_id") or "21m00Tcm4TlvDq8ikWAM"
        model    = c.get("elevenlabs_model")    or "eleven_flash_v2_5"
        r = _req.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json={"text": text, "model_id": model,
                  "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0}},
            timeout=30, stream=True,
        )
        r.raise_for_status()
        return Response((c for c in r.iter_content(4096)), mimetype="audio/mpeg",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # Edge TTS (free, no API key)
    import asyncio, edge_tts, io
    voice = c.get("edge_tts_voice") or "en-US-AriaNeural"

    async def _synthesise():
        buf = io.BytesIO()
        async for chunk in edge_tts.Communicate(text, voice).stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    try:
        loop  = asyncio.new_event_loop()
        audio = loop.run_until_complete(_synthesise())
        loop.close()
    except Exception as e:
        abort(500, str(e))

    return Response(audio, mimetype="audio/mpeg",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/answer")
def api_answer():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"answer": None, "provider": None})
    # stream=1 → Server-Sent Events; words appear as they generate
    if request.args.get("stream") == "1":
        return Response(
            stream_answer(q),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    answer, provider = get_answer(q)
    return jsonify({"answer": answer, "provider": provider})


# ── Export ──────────────────────────────────────────────────────────────────

@app.route("/api/export")
def api_export():
    query = request.args.get("q", "")
    fmt = request.args.get("format", "json")
    limit = min(int(request.args.get("limit", 1000)), 5000)
    data = search(query, page=1, per_page=limit)["results"]

    if fmt == "csv":
        fields = ["id", "title", "url", "media_type", "filetype",
                  "file_size", "file_path", "indexed_at"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data)
        buf.seek(0)
        fname = f"searchx-{query[:30] or 'export'}.csv".replace(" ", "_")
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})
    else:
        keys = ["id", "title", "url", "media_type", "filetype",
                "file_size", "file_path", "indexed_at"]
        return jsonify([{k: r.get(k) for k in keys} for r in data])


# ── Search History ──────────────────────────────────────────────────────────

@app.route("/history")
@login_required
def history_page():
    u = load_user()
    history = get_search_history(limit=200, user_id=u["id"] if u else None)
    return render_template("history.html", history=history)


@app.route("/api/history/clear", methods=["POST"])
@login_required
def api_history_clear():
    u = load_user()
    clear_search_history(user_id=u["id"] if u else None)
    return jsonify({"ok": True})


# ── Collections ─────────────────────────────────────────────────────────────

@app.route("/collections")
def collections_page():
    cols = get_collections()
    return render_template("collections.html", collections=cols)


@app.route("/collection/<int:cid>")
def collection_view(cid):
    col = get_collection(cid)
    if not col:
        abort(404)
    items = get_collection_items(cid)
    return render_template("collection.html", collection=col, items=items)


@app.route("/api/collection/create", methods=["POST"])
def api_collection_create():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    cid = create_collection(name, data.get("description", ""))
    return jsonify({"id": cid, "name": name})


@app.route("/api/collection/<int:cid>/add", methods=["POST"])
def api_collection_add(cid):
    data = request.json or {}
    doc_id = data.get("doc_id")
    if not doc_id:
        return jsonify({"error": "doc_id required"}), 400
    if not get_collection(cid):
        return jsonify({"error": "Collection not found"}), 404
    add_to_collection(cid, int(doc_id))
    return jsonify({"ok": True})


@app.route("/api/collection/<int:cid>/remove", methods=["POST"])
def api_collection_remove(cid):
    data = request.json or {}
    doc_id = data.get("doc_id")
    if doc_id:
        remove_from_collection(cid, int(doc_id))
    return jsonify({"ok": True})


@app.route("/collection/<int:cid>/delete", methods=["POST"])
def collection_delete(cid):
    delete_collection(cid)
    return redirect(url_for("collections_page"))


@app.route("/api/collection/<int:cid>/rename", methods=["POST"])
def api_collection_rename(cid):
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    rename_collection(cid, name, data.get("description", ""))
    return jsonify({"ok": True})


# ── Duplicates ───────────────────────────────────────────────────────────────

@app.route("/duplicates")
def duplicates_page():
    groups = get_duplicates()
    return render_template("duplicates.html", groups=groups)


# ── File watcher ─────────────────────────────────────────────────────────────

@app.route("/api/watcher/status")
def api_watcher_status():
    try:
        from watcher import is_watching, HAS_WATCHDOG
        return jsonify({"watching": is_watching(), "available": HAS_WATCHDOG})
    except Exception:
        return jsonify({"watching": False, "available": False})


@app.route("/api/watcher/start", methods=["POST"])
def api_watcher_start():
    from watcher import start_watcher
    sources = get_sources()
    ok = start_watcher(sources)
    return jsonify({"watching": ok})


@app.route("/api/watcher/stop", methods=["POST"])
def api_watcher_stop():
    from watcher import stop_watcher
    stop_watcher()
    return jsonify({"watching": False})


# ── Admin dashboard ──────────────────────────────────────────────────────────

@app.route("/analytics")
@login_required
def analytics():
    data = get_analytics()
    return render_template("analytics.html", data=data)


@app.route("/api/embed-missing", methods=["POST"])
@admin_required
def api_embed_missing():
    batch = min(int(request.json.get("batch", 100)), 500) if request.is_json else 100
    updated = embed_missing(batch=batch)
    return jsonify({"updated": updated})


@app.route("/admin")
@admin_required
def admin():
    from cache import search_cache
    stats = get_stats()
    conn  = get_connection()

    history_count   = conn.execute("SELECT COUNT(*) FROM search_history").fetchone()[0]
    collections_cnt = conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0]
    index_status    = get_index_status()

    try:
        from watcher import is_watching, HAS_WATCHDOG
        watcher = {"watching": is_watching(), "available": HAS_WATCHDOG}
    except Exception:
        watcher = {"watching": False, "available": False}

    db_size = conn.execute(
        "SELECT pg_database_size(current_database())"
    ).fetchone()[0]

    sys_info = {
        "python":   sys.version.split()[0],
        "platform": platform.system(),
        "cpus":     os.cpu_count(),
        "pid":      os.getpid(),
    }

    return render_template(
        "admin.html",
        stats=stats,
        cache=search_cache.stats,
        history_count=history_count,
        collections_count=collections_cnt,
        index_status=index_status,
        watcher=watcher,
        db_size=db_size,
        sys_info=sys_info,
    )


@app.route("/admin/rebuild-fts", methods=["POST"])
def admin_rebuild_fts():
    from database import rebuild_fts
    rebuild_fts()
    return jsonify({"ok": True, "message": "FTS index rebuilt"})


@app.route("/admin/clear-cache", methods=["POST"])
def admin_clear_cache():
    from cache import search_cache
    search_cache.invalidate()
    return jsonify({"ok": True, "message": "Cache cleared"})


@app.route("/admin/checkpoint", methods=["POST"])
def admin_checkpoint():
    conn = get_connection()
    conn.execute("ANALYZE")
    conn.commit()
    return jsonify({"ok": True, "message": "ANALYZE complete — query planner statistics refreshed"})


@app.route("/admin/pagerank", methods=["POST"])
def admin_pagerank():
    from database import compute_pagerank
    n = compute_pagerank()
    return jsonify({"ok": True, "message": f"PageRank computed for {n} documents"})


@app.route("/admin/optimize", methods=["POST"])
def admin_optimize():
    conn = get_connection()
    conn.execute("PRAGMA optimize")
    conn.execute("ANALYZE")
    conn.commit()
    return jsonify({"ok": True, "message": "Database statistics updated"})


@app.route("/admin/backup")
def admin_backup():
    """Dump the PostgreSQL database to a .sql file using pg_dump."""
    from datetime import datetime
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    fname = f"searchx_backup_{timestamp}.sql"
    tmp = tempfile.NamedTemporaryFile(suffix=".sql", delete=False)
    tmp.close()
    try:
        result = subprocess.run(
            ["pg_dump", DATABASE_URL, "-f", tmp.name],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            Path(tmp.name).unlink(missing_ok=True)
            return jsonify({"error": f"pg_dump failed: {result.stderr[:200]}"}), 500
        return send_file(tmp.name, as_attachment=True, download_name=fname)
    except FileNotFoundError:
        Path(tmp.name).unlink(missing_ok=True)
        return jsonify({"error": "pg_dump not found — install postgresql-client"}), 500
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


# ── Index Manager ─────────────────────────────────────────────────────────────

@app.route("/index-manager", methods=["GET", "POST"])
def index_manager():
    if request.method == "POST":
        local_paths = [
            p.strip() for p in request.form.get("local_paths", "").splitlines() if p.strip()
        ]
        web_urls = [
            u.strip() for u in request.form.get("web_urls", "").splitlines() if u.strip()
        ]
        options = {
            "depth":       int(request.form.get("depth", 2)),
            "max_pages":   int(request.form.get("max_pages", 200)),
            "same_domain": request.form.get("same_domain") == "on",
            "delay":       float(request.form.get("delay", 0.3)),
        }
        if local_paths or web_urls:
            run_index_job(local_paths, web_urls, options)
        return redirect(url_for("index_manager"))

    status = get_index_status()
    stats = get_stats()
    sources = get_sources()
    try:
        from watcher import is_watching, HAS_WATCHDOG
        watcher_status = {"watching": is_watching(), "available": HAS_WATCHDOG}
    except Exception:
        watcher_status = {"watching": False, "available": False}

    return render_template("index_manager.html", status=status, stats=stats,
                           sources=sources, home_dir=str(Path.home()),
                           watcher=watcher_status)


_HOME = Path.home().resolve()
_FS_ROOTS = [Path("/home").resolve(), Path("/media").resolve(),
             Path("/mnt").resolve(), _HOME]


@app.route("/api/browse")
def browse_directory():
    raw = request.args.get("path", str(_HOME))
    p = Path(raw).expanduser().resolve()
    if not any(str(p).startswith(str(r)) for r in _FS_ROOTS):
        p = _HOME
    if not p.exists() or not p.is_dir():
        p = _HOME
    dirs = []
    try:
        for item in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if item.is_dir():
                try:
                    item.stat()
                    dirs.append({"name": item.name, "path": str(item),
                                 "hidden": item.name.startswith(".")})
                except PermissionError:
                    pass
    except PermissionError:
        pass
    parent = str(p.parent) if p != p.parent else None
    return jsonify({"current": str(p), "parent": parent, "dirs": dirs})


# ── Source management ─────────────────────────────────────────────────────────

@app.route("/source/<int:sid>/settings", methods=["GET", "POST"])
def source_settings(sid):
    src = get_source(sid)
    if not src:
        abort(404)
    saved = False
    if request.method == "POST":
        label = request.form.get("label", src["path"])
        s = dict(src["settings"])
        if src["type"] == "web":
            s["depth"]       = int(request.form.get("depth", 2))
            s["max_pages"]   = int(request.form.get("max_pages", 200))
            s["same_domain"] = request.form.get("same_domain") == "on"
            s["delay"]       = float(request.form.get("delay", 0.3))
        else:
            exts = request.form.get("extensions", "")
            s["extensions"] = [e.strip().lstrip(".") for e in
                               exts.replace(",", "\n").splitlines() if e.strip()]
        update_source_settings(sid, label, s)
        saved = True
        src = get_source(sid)
    return render_template("source_settings.html", src=src, saved=saved)


@app.route("/source/<int:sid>/reindex", methods=["POST"])
def source_reindex(sid):
    src = get_source(sid)
    if not src:
        abort(404)
    s = src["settings"]
    if src["type"] == "local":
        run_index_job([src["path"]], [], {})
    else:
        run_index_job([], [src["path"]], {
            "depth":       s.get("depth", 2),
            "max_pages":   s.get("max_pages", 200),
            "same_domain": s.get("same_domain", True),
            "delay":       s.get("delay", 0.3),
        })
    return redirect(url_for("index_manager"))


@app.route("/source/<int:sid>/toggle", methods=["POST"])
def source_toggle(sid):
    src = get_source(sid)
    if src:
        toggle_source(sid, not src["enabled"])
    return redirect(url_for("index_manager"))


@app.route("/source/<int:sid>/delete", methods=["POST"])
def source_delete(sid):
    delete_source(sid)
    return redirect(url_for("index_manager"))


# ── Settings ──────────────────────────────────────────────────────────────────

def _human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


@app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    saved = False
    if request.method == "POST":
        cfg = cfg_module.load()   # start from current values so no key is lost
        cfg["open_allowed"] = [
            e.strip().lstrip(".")
            for e in request.form.get("open_allowed", "").replace(",", "\n").splitlines()
            if e.strip().lstrip(".")
        ]
        cfg["open_denied"] = [
            e.strip().lstrip(".")
            for e in request.form.get("open_denied", "").replace(",", "\n").splitlines()
            if e.strip().lstrip(".")
        ]
        cfg["auto_crawl_enabled"]        = request.form.get("auto_crawl_enabled") == "on"
        cfg["auto_crawl_interval_hours"] = int(request.form.get("auto_crawl_interval_hours", 24))

        def _int(key, default):
            v = request.form.get(key, "").strip()
            try:
                return int(float(v)) if v else default
            except (ValueError, TypeError):
                return default

        def _float(key, default):
            v = request.form.get(key, "").strip()
            try:
                return float(v) if v else default
            except (ValueError, TypeError):
                return default

        # Crawler network settings
        cfg["crawler_max_concurrent"]  = max(1,   _int("crawler_max_concurrent",  500))
        cfg["crawler_max_per_domain"]  = max(1,   _int("crawler_max_per_domain",  3))
        cfg["crawler_delay"]           = max(0.1, _float("crawler_delay",         1.0))
        cfg["crawler_db_workers"]      = max(1,   _int("crawler_db_workers",      32))
        cfg["crawler_connector_limit"] = max(10,  _int("crawler_connector_limit", 1000))
        cfg["crawler_dns_ttl"]         = max(30,  _int("crawler_dns_ttl",         600))
        cfg["crawler_domain_budget"]   = max(1,   _int("crawler_domain_budget",   500))
        cfg["crawler_screenshots"]     = request.form.get("crawler_screenshots") == "on"

        # TTS
        cfg["tts_provider"]        = request.form.get("tts_provider", "edge").strip()
        cfg["edge_tts_voice"]      = request.form.get("edge_tts_voice", "en-US-AriaNeural").strip()
        cfg["elevenlabs_api_key"]  = request.form.get("elevenlabs_api_key", "").strip()
        cfg["elevenlabs_model"]    = request.form.get("elevenlabs_model", "eleven_flash_v2_5").strip()
        # The form has two inputs named elevenlabs_voice_id (select + text); take the last non-empty one
        voice_ids = [v.strip() for v in request.form.getlist("elevenlabs_voice_id") if v.strip() and v.strip() != "custom"]
        if voice_ids:
            cfg["elevenlabs_voice_id"] = voice_ids[-1]

        # Extension settings
        cfg["extension_api_key"]       = request.form.get("extension_api_key", "").strip()

        # AI answer box — mode + provider + all keys/models
        cfg["ai_answer_mode"]   = request.form.get("ai_answer_mode", "results").strip()
        cfg["ai_provider"]      = request.form.get("ai_provider", "ollama").strip()
        cfg["anthropic_api_key"]= request.form.get("anthropic_api_key", "").strip()
        cfg["anthropic_model"]  = request.form.get("anthropic_model", "claude-haiku-4-5-20251001").strip()
        cfg["openai_api_key"]   = request.form.get("openai_api_key", "").strip()
        cfg["openai_model"]     = request.form.get("openai_model", "gpt-4o-mini").strip()
        cfg["gemini_api_key"]   = request.form.get("gemini_api_key", "").strip()
        cfg["gemini_model"]     = request.form.get("gemini_model", "gemini-1.5-flash").strip()
        cfg["ollama_url"]       = request.form.get("ollama_url", "http://localhost:11434").strip()
        cfg["ollama_model"]     = request.form.get("ollama_model", "llama3.2").strip()

        cfg_module.save(cfg)
        # Clear answer cache so mode/provider changes take effect immediately
        from cache import answer_cache
        answer_cache.invalidate()
        saved = True
    if request.args.get("reset"):
        cfg_module.save(dict(cfg_module.DEFAULTS))
        return redirect(url_for("settings"))
    current = cfg_module.load()

    # Compute human-readable crawl status for the template
    web_source_count = sum(
        1 for s in get_sources() if s["type"] == "web" and s.get("enabled", True)
    )
    last_crawl  = current.get("auto_crawl_last", 0)
    interval_s  = current.get("auto_crawl_interval_hours", 24) * 3600
    next_crawl  = (last_crawl + interval_s) if last_crawl else 0
    now         = time.time()

    crawl_status = {
        "web_source_count": web_source_count,
        "last_crawl_ts":    last_crawl,
        "last_crawl_ago":   _human_duration(now - last_crawl) if last_crawl else "Never",
        "next_crawl_in":    _human_duration(max(0, next_crawl - now)) if last_crawl else "On next save",
        "overdue":          now > next_crawl and last_crawl > 0,
    }
    return render_template("settings.html", cfg=current, saved=saved,
                           crawl_status=crawl_status, active_page="settings")


# ── Auto-crawl API ───────────────────────────────────────────────────────────

@app.route("/api/auto-crawl/trigger", methods=["POST"])
def api_auto_crawl_trigger():
    web_sources = [
        s for s in get_sources()
        if s["type"] == "web" and s.get("enabled", True)
    ]
    if not web_sources:
        return jsonify({"ok": False, "message": "No enabled web sources to crawl"}), 400
    run_index_job([], [s["path"] for s in web_sources], {
        "depth": 2, "max_pages": 200, "same_domain": True, "delay": 0.5,
    })
    return jsonify({"ok": True, "message": f"Crawl started on {len(web_sources)} source(s)"})


@app.route("/api/auto-crawl/status")
def api_auto_crawl_status():
    cfg        = cfg_module.load()
    last       = cfg.get("auto_crawl_last", 0)
    interval_s = cfg.get("auto_crawl_interval_hours", 24) * 3600
    now        = time.time()
    return jsonify({
        "enabled":       cfg.get("auto_crawl_enabled", False),
        "interval_hours": cfg.get("auto_crawl_interval_hours", 24),
        "last_crawl_ago": _human_duration(now - last) if last else "Never",
        "next_crawl_in":  _human_duration(max(0, last + interval_s - now)) if last else "On next save",
    })


# ── Cached page viewer ───────────────────────────────────────────────────────

def _load_doc(doc_id: int) -> dict | None:
    """Fetch a document row and normalise types."""
    conn = get_connection()
    row  = conn.execute("SELECT * FROM documents WHERE id = %s", (doc_id,)).fetchone()
    if not row:
        return None
    doc = dict(row)
    if not isinstance(doc.get("metadata"), dict):
        doc["metadata"] = {}
    for k, v in doc.items():
        if hasattr(v, "isoformat"):
            doc[k] = v.isoformat()
    return doc


@app.route("/cached/<int:doc_id>")
def cached_page(doc_id):
    doc = _load_doc(doc_id)
    if not doc:
        abort(404)
    return render_template("cached.html", doc=doc)


@app.route("/cached/<int:doc_id>/archive")
def cached_archive(doc_id):
    """Fetch the live page and all its assets, rewrite links, return as ZIP."""
    import base64
    import hashlib
    import io
    import mimetypes
    import re
    import zipfile
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from urllib.parse import urlparse, urljoin
    import requests as req
    from lxml import html as lxml_html
    from lxml.etree import ParserError

    _MAX_ASSETS      = 150
    _MAX_ASSET_BYTES = 8 * 1024 * 1024    # 8 MB per asset
    _MAX_VIDEO_BYTES = 100 * 1024 * 1024  # 100 MB per video
    _TIMEOUT         = 12
    _UA              = "SearchXBot/1.0 (archive; +http://localhost)"

    doc = _load_doc(doc_id)
    if not doc:
        abort(404)

    live_url = doc.get("url", "")
    if not live_url:
        abort(400)

    # ── 1. Fetch live HTML ────────────────────────────────────────────────────
    try:
        resp = req.get(live_url, timeout=_TIMEOUT,
                       headers={"User-Agent": _UA}, allow_redirects=True)
        resp.raise_for_status()
        raw_html   = resp.content
        final_url  = resp.url
        encoding   = resp.apparent_encoding or "utf-8"
    except Exception as exc:
        return jsonify({"error": f"Could not fetch live page: {exc}"}), 502

    # ── 2. Parse HTML + resolve all links ────────────────────────────────────
    try:
        tree = lxml_html.fromstring(raw_html, base_url=final_url)
        tree.make_links_absolute(final_url, resolve_base_href=True)
    except (ParserError, ValueError) as exc:
        return jsonify({"error": f"HTML parse error: {exc}"}), 500

    # ── 3. Collect asset URLs ─────────────────────────────────────────────────
    seen: set[str] = set()
    asset_urls: list[tuple[str, str]] = []  # (url, hint)

    def _add(url: str, hint: str = "asset"):
        if not url or url.startswith("data:") or url in seen:
            return
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return
        seen.add(url)
        asset_urls.append((url, hint))

    for href in tree.xpath("//link[@rel='stylesheet']/@href"):
        _add(href, "css")
    for src in tree.xpath("//script/@src"):
        _add(src, "js")
    for src in tree.xpath("//img/@src"):
        _add(src, "image")
    for src in tree.xpath("//img/@data-src"):
        _add(src, "image")
    for src in tree.xpath("//source/@src"):
        _add(src, "media")
    for src in tree.xpath("//video/@src"):
        _add(src, "media")
    for src in tree.xpath("//audio/@src"):
        _add(src, "media")
    for href in tree.xpath("//link[@rel='icon' or @rel='shortcut icon' or @rel='apple-touch-icon']/@href"):
        _add(href, "image")

    asset_urls = asset_urls[:_MAX_ASSETS]

    # ── 4. Download assets in parallel ───────────────────────────────────────
    def _local_name(url: str, hint: str) -> str:
        ext = (Path(urlparse(url).path).suffix or "").lower()[:10]
        if not ext:
            guessed, _ = mimetypes.guess_type(url)
            if guessed:
                ext = mimetypes.guess_extension(guessed) or ""
        h = hashlib.md5(url.encode()).hexdigest()[:12]
        return f"assets/{h}{ext}"

    def _fetch_asset(item):
        url, hint = item
        local = _local_name(url, hint)
        max_b = _MAX_VIDEO_BYTES if hint == "media" else _MAX_ASSET_BYTES
        try:
            r = req.get(url, timeout=_TIMEOUT,
                        headers={"User-Agent": _UA},
                        stream=True, allow_redirects=True)
            r.raise_for_status()
            chunks = []
            size = 0
            for chunk in r.iter_content(65536):
                size += len(chunk)
                if size > max_b:
                    return url, local, None   # too large — link stays external
                chunks.append(chunk)
            return url, local, b"".join(chunks)
        except Exception:
            return url, local, None

    url_to_local: dict[str, str] = {}
    asset_data:   dict[str, bytes] = {}

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_fetch_asset, item): item for item in asset_urls}
        for fut in as_completed(futures):
            orig_url, local, data = fut.result()
            if data is not None:
                url_to_local[orig_url] = local
                asset_data[local]      = data

    # ── 5. Rewrite HTML links to local paths ─────────────────────────────────
    def _rewrite(url: str) -> str:
        return url_to_local.get(url, url)   # keep external if not downloaded

    tree.rewrite_links(_rewrite)

    # ── 6. Inject offline banner ──────────────────────────────────────────────
    from lxml import etree
    banner_html = (
        f'<div style="position:sticky;top:0;z-index:99999;background:#1a1a2e;'
        f'color:#e8e8e8;font-family:sans-serif;font-size:.82rem;padding:.5rem 1rem;'
        f'border-bottom:2px solid #4f8ef7;display:flex;align-items:center;gap:1rem;">'
        f'<strong style="color:#4f8ef7">SearchX Archive</strong>'
        f'<span>Saved copy of <a href="{live_url}" style="color:#4f8ef7">{live_url}</a></span>'
        f'<span style="margin-left:auto;color:#888">'
        f'Cached {(doc.get("indexed_at") or "")[:19]} UTC</span>'
        f'</div>'
    )
    try:
        body = tree.body
        if body is not None:
            banner_el = lxml_html.fragment_fromstring(banner_html)
            body.insert(0, banner_el)
    except Exception:
        pass

    final_html = lxml_html.tostring(tree, encoding="unicode",
                                    doctype="<!DOCTYPE html>")

    # ── 7. Package as ZIP ─────────────────────────────────────────────────────
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("index.html", final_html.encode("utf-8", errors="replace"))
        for local, data in asset_data.items():
            zf.writestr(local, data)

    buf.seek(0)
    safe_title = re.sub(r"[^\w\-.]", "_", (doc.get("title") or "page")[:60])
    fname = f"SearchX_{safe_title}.zip"

    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=fname,
    )


@app.route("/cached/<int:doc_id>/download")
def cached_download(doc_id):
    import base64
    import re
    import requests as req

    doc = _load_doc(doc_id)
    if not doc:
        abort(404)

    # Embed images as base64 data URIs so they work fully offline (cap at 15)
    embedded_images = []
    for img in doc["metadata"].get("images", [])[:15]:
        try:
            r = req.get(img["src"], timeout=5,
                        headers={"User-Agent": "SearchXBot/1.0"}, stream=False)
            if r.ok:
                ct  = r.headers.get("content-type", "image/jpeg").split(";")[0]
                b64 = base64.b64encode(r.content).decode()
                embedded_images.append({**img, "src": f"data:{ct};base64,{b64}"})
            else:
                embedded_images.append(img)
        except Exception:
            embedded_images.append(img)   # fallback: keep original URL

    html = render_template("cached_download.html",
                           doc=doc, embedded_images=embedded_images)

    safe_title = re.sub(r"[^\w\-.]", "_", (doc["title"] or "page")[:60])
    fname = f"SearchX_{safe_title}.html"

    return Response(
        html,
        mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── File serving ──────────────────────────────────────────────────────────────

@app.route("/stream")
def stream_file():
    path = request.args.get("path", "")
    ok, result = validate_indexed_path(path)
    if not ok:
        abort(403)
    allowed, reason = cfg_module.is_open_allowed(result)
    if not allowed:
        abort(403)
    return send_file(result, conditional=True)


@app.route("/view/<int:doc_id>")
def view_media(doc_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM documents WHERE id = %s", (doc_id,)).fetchone()
    if not row:
        abort(404)
    doc = dict(row)
    if not isinstance(doc.get("metadata"), dict):
        doc["metadata"] = {}
    for k, v in doc.items():
        if hasattr(v, "isoformat"):
            doc[k] = v.isoformat()
    return render_template("viewer.html", doc=doc)


@app.route("/open")
def open_file():
    path = request.args.get("path", "")
    ok, result = validate_indexed_path(path)
    if not ok:
        return jsonify({"error": result}), 403
    allowed, reason = cfg_module.is_open_allowed(result)
    if not allowed:
        return jsonify({"error": reason}), 403
    subprocess.Popen(["xdg-open", result])
    return ("", 204)


# ── Monitoring APIs ───────────────────────────────────────────────────────────

@app.route("/api/index-status")
def api_index_status():
    return jsonify(get_index_status())


@app.route("/api/index-single", methods=["POST"])
def api_index_single():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "No URL provided"}), 400
    result = index_single_url(url)
    return jsonify(result)


@app.route("/api/cache-stats")
def api_cache_stats():
    from cache import search_cache
    return jsonify(search_cache.stats)


@app.route("/api/health")
def api_health():
    try:
        conn = get_connection()
        total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        return jsonify({"status": "ok", "documents": total}), 200
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 503


# ── Web crawler dashboard ─────────────────────────────────────────────────────

@app.route("/crawler")
def crawler_page():
    status = get_crawler_status()
    return render_template("crawler.html", status=status)


@app.route("/api/crawler/start", methods=["POST"])
def api_crawler_start():
    data  = request.json or {}
    seeds = [u.strip() for u in data.get("seeds", []) if u.strip()]
    delay = float(data.get("delay", 1.0))
    ok = start_crawler(seed_urls=seeds or None, delay=delay)
    msg = "Crawler started" if ok else "Crawler is already running"
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/crawler/stop", methods=["POST"])
def api_crawler_stop():
    stop_crawler()
    return jsonify({"ok": True, "message": "Crawler stopping…"})


@app.route("/api/crawler/status")
def api_crawler_status():
    return jsonify(get_crawler_status())


@app.route("/api/crawler/clear", methods=["POST"])
def api_crawler_clear():
    from database import clear_queue
    clear_queue()
    return jsonify({"ok": True, "message": "Queue cleared"})


@app.route("/api/crawler/seed", methods=["POST"])
def api_crawler_seed():
    """Add seed URLs to the queue without starting the crawler."""
    data  = request.json or {}
    seeds = [u.strip() for u in data.get("seeds", []) if u.strip()]
    if not seeds:
        return jsonify({"error": "No URLs provided"}), 400
    from database import queue_urls
    queue_urls(seeds, depth=0)
    return jsonify({"ok": True, "queued": len(seeds)})


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if load_user():
        return redirect(url_for("index"))
    if request.method == "GET":
        return render_template("login.html", next=request.args.get("next", ""))
    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")
    next_url = request.form.get("next", "") or url_for("index")
    user = get_user_by_username(username)
    if not user or not verify_password(password, user["password_hash"]):
        return render_template("login.html", error="Invalid username or password.",
                               username=username, next=next_url), 401
    resp = redirect(next_url if next_url.startswith("/") else url_for("index"))
    set_auth_cookie(resp, user["id"], user["role"])
    return resp


@app.route("/register", methods=["GET", "POST"])
def register_page():
    if load_user():
        return redirect(url_for("index"))
    if request.method == "GET":
        return render_template("register.html")
    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")
    password2 = request.form.get("password2", "")
    import re as _re
    if not _re.match(r'^[a-z0-9_]{3,32}$', username):
        return render_template("register.html", error="Username must be 3–32 chars: letters, numbers, underscore.", username=username), 400
    if len(password) < 6:
        return render_template("register.html", error="Password must be at least 6 characters.", username=username), 400
    if password != password2:
        return render_template("register.html", error="Passwords do not match.", username=username), 400
    uid = create_user(username, hash_password(password))
    if uid is None:
        return render_template("register.html", error="Username already taken.", username=username), 409
    resp = redirect(url_for("index"))
    set_auth_cookie(resp, uid, "user")
    return resp


@app.route("/logout", methods=["GET", "POST"])
def logout():
    resp = redirect(url_for("login_page"))
    clear_auth_cookie(resp)
    return resp


# ── Account routes ─────────────────────────────────────────────────────────────

@app.route("/account")
@login_required
def account_page():
    return render_template("account.html", active_page="account")


@app.route("/account/change-password", methods=["POST"])
@login_required
def account_change_password():
    u = load_user()
    user = get_user_by_id(u["id"])
    current_pw  = request.form.get("current_password", "")
    new_pw      = request.form.get("new_password", "")
    new_pw2     = request.form.get("new_password2", "")
    if not verify_password(current_pw, user["password_hash"]):
        return render_template("account.html", error="Current password is incorrect.", active_page="account"), 400
    if len(new_pw) < 6:
        return render_template("account.html", error="New password must be at least 6 characters.", active_page="account"), 400
    if new_pw != new_pw2:
        return render_template("account.html", error="New passwords do not match.", active_page="account"), 400
    update_password(u["id"], hash_password(new_pw))
    return render_template("account.html", saved="Password updated successfully.", active_page="account")


@app.route("/account/history")
@login_required
def account_history():
    u = load_user()
    history = get_search_history(limit=500, user_id=u["id"])
    return render_template("account_history.html", history=history, active_page="history")


# ── Admin user management ─────────────────────────────────────────────────────

@app.route("/admin/users")
@admin_required
def admin_users():
    users = get_all_users()
    return render_template("admin_users.html", users=users, active_page="users")


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_users_create():
    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")
    role     = request.form.get("role", "user")
    if role not in ("admin", "user"):
        role = "user"
    uid = create_user(username, hash_password(password), role=role)
    if uid is None:
        users = get_all_users()
        return render_template("admin_users.html", users=users, active_page="users",
                               error=f"Username '{username}' is already taken."), 409
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_users_delete(uid):
    me = load_user()
    if uid == me["id"]:
        users = get_all_users()
        return render_template("admin_users.html", users=users, active_page="users",
                               error="You cannot delete your own account."), 400
    delete_user(uid)
    return redirect(url_for("admin_users"))


# ── Browser extension API ─────────────────────────────────────────────────────

@app.route("/api/extension/add", methods=["POST"])
def api_extension_add():
    data = request.json or {}
    api_key = data.get("api_key", "")
    cfg = cfg_module.load()
    if cfg.get("extension_api_key") and api_key != cfg["extension_api_key"]:
        return jsonify({"error": "Invalid API key"}), 403
    url = data.get("url", "").strip()
    title = data.get("title", "").strip()
    content = data.get("content", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    from database import upsert_document
    upsert_document(url=url, title=title or url, content=content,
                    filetype="html", media_type="web", source="extension", metadata={})
    return jsonify({"ok": True, "message": f"Added: {title or url}"})


if __name__ == "__main__":
    debug = os.environ.get("SEARCHX_DEBUG", "0") == "1"
    app.run(debug=debug, port=5000, threaded=True, host="127.0.0.1")
