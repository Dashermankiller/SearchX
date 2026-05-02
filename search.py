import logging
import re
from urllib.parse import urlparse
from database import get_connection, did_you_mean

log = logging.getLogger(__name__)
from security import safe_snippet
from cache import search_cache, answer_cache

def _extract_base_url(url: str) -> str:
    """Return just the host, e.g. 'https://www.github.com/x' → 'github.com'."""
    try:
        host = urlparse(url).netloc.lower()
        return host.removeprefix("www.")
    except Exception:
        return url


def _url_breadcrumb(url: str) -> str:
    """
    Return a Google-style breadcrumb, e.g.:
    'https://github.com/user/repo' → 'github.com › user › repo'
    """
    try:
        p     = urlparse(url)
        host  = p.netloc.lower().removeprefix("www.")
        parts = [seg for seg in p.path.split("/") if seg]
        if not parts:
            return host
        # Truncate very long segments
        crumbs = [seg[:30] + "…" if len(seg) > 30 else seg for seg in parts[:4]]
        return host + " › " + " › ".join(crumbs)
    except Exception:
        return url


MEDIA_TYPE_MAP = {
    "image":    {"jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "tiff"},
    "audio":    {"mp3", "wav", "flac", "ogg", "aac", "m4a"},
    "video":    {"mp4", "mkv", "avi", "mov", "webm", "m4v"},
    "document": {"pdf", "docx", "doc", "txt", "xlsx", "xls", "pptx", "ppt", "csv"},
    "web":      {"html", "htm"},
}

FLAG_ALIASES = {
    "ext":    "filetype",
    "kind":   "type",
    "domain": "site",
    "from":   "source",
    "url":    "inurl",
    "title":  "intitle",
}

SORT_MAP = {
    "date_desc": "d.indexed_at DESC",
    "date_asc":  "d.indexed_at ASC",
    "size_desc": "d.file_size DESC",
    "size_asc":  "d.file_size ASC",
    "name_asc":  "LOWER(d.title) ASC",
    "name_desc": "LOWER(d.title) DESC",
}


def _normalise_date(value: str, end_of_period: bool = False) -> str:
    """
    Convert user-supplied date values into ISO dates Postgres can compare.
      '2024'      → '2024-01-01'  (or '2024-12-31' when end_of_period)
      '2024-06'   → '2024-06-01'  (or '2024-06-30')
      '2024-06-15'→ '2024-06-15'
    """
    v = value.strip()
    if re.fullmatch(r'\d{4}', v):
        return f"{v}-12-31" if end_of_period else f"{v}-01-01"
    if re.fullmatch(r'\d{4}-\d{2}', v):
        year, month = int(v[:4]), int(v[5:7])
        if end_of_period:
            import calendar
            last = calendar.monthrange(year, month)[1]
            return f"{v}-{last:02d}"
        return f"{v}-01"
    return v   # already a full date or unknown — pass through


def parse_query(raw: str) -> dict:
    """
    Parse a user query string into structured components.

    Supported operators:
      site:example.com      — restrict to domain (exact host match)
      filetype:pdf          — restrict by file extension
      intitle:word          — title must contain word
      inurl:slug            — URL must contain slug
      type:video            — restrict by media type
      source:local          — restrict by ingestion source
      before:2024           — indexed before date (year, year-month, or full date)
      after:2023-06         — indexed after date
      -word                 — exclude word
      "exact phrase"        — exact phrase match

    Returns a dict with: terms, phrases, excludes, flags, clean_query, active_operators
    """
    flags = {}
    phrases = []
    excludes = []
    active_operators = []   # human-readable list for the UI

    # Extract quoted phrases first (preserve as-is for FTS)
    for p in re.findall(r'"([^"]+)"', raw):
        phrases.append(p.lower())

    # Extract key:value operators (supports quoted values: key:"multi word")
    clean = raw
    for m in re.finditer(r'(\w+)[:=]"([^"]+)"|(\w+)[:=](\S+)', raw):
        if m.group(1):
            k, v = m.group(1).lower(), m.group(2)
        else:
            k, v = m.group(3).lower(), m.group(4)
        k = FLAG_ALIASES.get(k, k)
        flags[k] = v

    clean = re.sub(r'\w+[:=]"[^"]*"', " ", clean)
    clean = re.sub(r'\w+[:=]\S+',     " ", clean).strip()

    # Normalise before:/after: date values
    for op in ("before", "after"):
        if op in flags:
            flags[op] = _normalise_date(flags[op], end_of_period=(op == "before"))

    # Build active_operators list for UI display
    op_labels = {
        "site": "site", "filetype": "filetype", "intitle": "intitle",
        "inurl": "inurl", "type": "type", "source": "source",
        "before": "before", "after": "after",
    }
    for k, v in flags.items():
        if k in op_labels:
            active_operators.append({"key": k, "value": v})

    # Exclusion words (-word)
    for t in re.findall(r'-(\w+)', clean):
        excludes.append(t.lower())
    clean = re.sub(r'-\w+', " ", clean).strip()

    # Plain terms for ILIKE fallback + snippet highlighting
    tmp   = re.sub(r'"[^"]+"', " ", clean)
    terms = [t.lower() for t in tmp.split() if t]

    return {
        "terms":            terms,
        "phrases":          phrases,
        "excludes":         excludes,
        "flags":            flags,
        "clean_query":      clean,
        "active_operators": active_operators,
    }


def search(query_str: str, page: int = 1, per_page: int = 10, sort: str = "date_desc") -> dict:
    cache_key = f"{query_str}|{page}|{per_page}|{sort}"
    cached = search_cache.get(cache_key)
    if cached is not None:
        return cached

    parsed = parse_query(query_str)
    flags  = parsed["flags"]

    conditions: list[str] = []
    params:     list      = []

    # ── Full-text search ───────────────────────────────────────────────────────
    fts_query = parsed["clean_query"]
    all_terms = parsed["terms"] + parsed["phrases"]

    if fts_query:
        fts_clause = "d.search_vector @@ websearch_to_tsquery('english', %s)"
        if all_terms:
            like_parts, like_params = [], []
            for t in all_terms:
                like_parts.append("(d.title ILIKE %s OR d.url ILIKE %s)")
                like_params += [f"%{t}%", f"%{t}%"]
            conditions.append(
                f"({fts_clause} OR ({' AND '.join(like_parts)}))"
            )
            params.append(fts_query)
            params.extend(like_params)
        else:
            conditions.append(fts_clause)
            params.append(fts_query)

    # ── Operator filters ───────────────────────────────────────────────────────

    # filetype:pdf  or  filetype:.pdf
    if "filetype" in flags:
        conditions.append("LOWER(d.filetype) = %s")
        params.append(flags["filetype"].lstrip(".").lower())

    # type:video
    if "type" in flags:
        conditions.append("d.media_type = %s")
        params.append(flags["type"].lower())

    # site:example.com — use the base_url functional index for fast exact host match
    if "site" in flags:
        raw_site = flags["site"].lower().lstrip("www.")
        conditions.append(
            "LOWER(REGEXP_REPLACE(d.url, '^https?://(www\\.)?([^/?#]+).*$', '\\2', 'i')) = %s"
        )
        params.append(raw_site)

    # source:webcrawler / source:local
    if "source" in flags:
        conditions.append("d.source = %s")
        params.append(flags["source"].lower())

    # intitle:word  — title must contain the word (FTS on title for ranking bonus too)
    if "intitle" in flags:
        conditions.append(
            "(d.title ILIKE %s OR d.search_vector @@ to_tsquery('english', %s))"
        )
        val = flags["intitle"]
        conditions[-1]   # already appended
        params.append(f"%{val}%")
        # sanitise for to_tsquery: keep only word chars
        ts_val = re.sub(r'\W+', ' ', val).strip().replace(' ', ' & ')
        params.append(ts_val if ts_val else val)

    # inurl:slug
    if "inurl" in flags:
        conditions.append("d.url ILIKE %s")
        params.append(f"%{flags['inurl']}%")

    # before:YYYY-MM-DD / after:YYYY-MM-DD
    if "before" in flags:
        conditions.append("d.indexed_at < %s::date")
        params.append(flags["before"])

    if "after" in flags:
        conditions.append("d.indexed_at > %s::date")
        params.append(flags["after"])

    # ── Build SQL ──────────────────────────────────────────────────────────────
    where    = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sort_col = SORT_MAP.get(sort, SORT_MAP["date_desc"])
    offset   = (page - 1) * per_page

    # Detect domain/URL queries (e.g. "google.com", "github.com/user")
    # so we can strongly boost pages whose URL matches
    _domain_boost_sql    = ""
    _domain_boost_params = []
    if fts_query and not flags:
        clean = fts_query.strip().lower()
        if re.match(r'^(https?://)?[\w.-]+\.[a-z]{2,}(/\S*)?$', clean) and ' ' not in clean:
            # normalise — strip protocol
            domain_pat = re.sub(r'^https?://', '', clean)
            _domain_boost_sql    = "+ 10.0 * (CASE WHEN LOWER(d.url) LIKE %s THEN 1 ELSE 0 END)"
            _domain_boost_params = [f"%{domain_pat}%"]

    if fts_query:
        rank_col = (
            "ts_rank_cd(d.search_vector, websearch_to_tsquery('english', %s))"
            f" * (1.0 + 3.0 * COALESCE(d.pagerank, 0))"
            f" {_domain_boost_sql} AS _rank"
        )
        rank_params = [fts_query] + _domain_boost_params
        order_by    = f"_rank DESC, {sort_col}"
    else:
        rank_col    = "COALESCE(d.pagerank, 0) AS _rank"
        rank_params = []
        order_by    = f"_rank DESC, {sort_col}"

    conn = get_connection()

    total = conn.execute(
        f"SELECT COUNT(*) FROM documents d {where}", params
    ).fetchone()[0]

    # For semantic reranking fetch a larger candidate pool on page 1
    semantic_pool = per_page * 5 if (fts_query and page == 1) else per_page
    rows = conn.execute(
        f"""SELECT d.*, {rank_col}
            FROM documents d {where}
            ORDER BY {order_by}
            LIMIT %s OFFSET %s""",
        rank_params + params + [semantic_pool, offset],
    ).fetchall()

    results = []
    for row in rows:
        r = dict(row)
        if not isinstance(r.get("metadata"), dict):
            r["metadata"] = {}
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                r[k] = v.isoformat()
        content         = r.get("content") or ""
        highlight_terms = all_terms if fts_query else []
        r["snippet"]    = safe_snippet(content, highlight_terms)
        r["base_url"]   = _extract_base_url(r.get("url", ""))
        r["url_crumb"]  = _url_breadcrumb(r.get("url", ""))
        results.append(r)

    # ── Semantic reranking (hybrid) ────────────────────────────────────────────
    if fts_query and page == 1 and results:
        try:
            from embeddings import embed as _embed, rerank as _rerank
            q_vec = _embed(fts_query)
            if q_vec:
                results = _rerank(q_vec, results)[:per_page]
        except Exception as _se:
            log.debug("Semantic rerank skipped: %s", _se)
            results = results[:per_page]
    else:
        results = results[:per_page]

    # ── Spelling correction ────────────────────────────────────────────────────
    suggestion = None
    if fts_query and not flags:   # only suggest when no operators are active
        try:
            suggestion = did_you_mean(fts_query, total)
        except Exception:
            pass

    result = {
        "results":          results,
        "total":            total,
        "page":             page,
        "per_page":         per_page,
        "total_pages":      max(1, (total + per_page - 1) // per_page),
        "query":            query_str,
        "parsed":           parsed,
        "sort":             sort,
        "suggestion":       suggestion,
        "active_operators": parsed["active_operators"],
    }
    search_cache.set(cache_key, result)
    return result


def get_answer(query_str: str) -> tuple[str | None, str | None]:
    """Return (answer_text, provider_label), using answer_cache for speed."""
    cached = answer_cache.get(query_str)
    if cached is not None:
        return cached

    parsed = parse_query(query_str)
    if not parsed["clean_query"]:
        return None, None

    try:
        import config as _cfg
        mode = _cfg.load().get("ai_answer_mode", "results")
    except Exception:
        mode = "results"

    # Re-use the first page of results (almost always cached at this point)
    result = search(query_str, page=1, per_page=10)
    results = result["results"]

    if mode == "results" and not results:
        return None, None

    answer, provider = get_answer_box(query_str, results, mode)
    answer_cache.set(query_str, (answer, provider))
    return answer, provider


def stream_answer(query_str: str):
    """
    Generator that yields SSE-formatted strings.
    First yields {"provider": label}, then {"text": chunk} per token, then {"done": true}.
    Falls back to the cached/non-streaming path if already cached.
    """
    # Serve from cache instantly if available
    cached = answer_cache.get(query_str)
    if cached is not None:
        answer, provider = cached
        if answer:
            import json as _json
            yield f"data: {_json.dumps({'provider': provider})}\n\n"
            yield f"data: {_json.dumps({'text': answer})}\n\n"
        yield "data: {\"done\": true}\n\n"
        return

    parsed = parse_query(query_str)
    if not parsed["clean_query"]:
        yield "data: {\"done\": true}\n\n"
        return

    try:
        import config as _cfg
        cfg  = _cfg.load()
        mode = cfg.get("ai_answer_mode", "results")
    except Exception:
        cfg  = {}
        mode = "results"

    result  = search(query_str, page=1, per_page=10)
    results = result["results"]
    if mode == "results" and not results:
        yield "data: {\"done\": true}\n\n"
        return

    prompt   = _build_prompt(query_str, results, mode)
    selected = cfg.get("ai_provider", "ollama")
    order    = [selected] + [p for p in _STREAM_PROVIDERS if p != selected]

    import json as _json
    for provider_key in order:
        stream_fn = _STREAM_PROVIDERS.get(provider_key)
        if not stream_fn:
            continue
        try:
            label  = _PROVIDER_LABELS.get(provider_key, provider_key)
            chunks = []
            first  = True
            for chunk in stream_fn(prompt, cfg):
                if first:
                    yield f"data: {_json.dumps({'provider': label})}\n\n"
                    first = False
                chunks.append(chunk)
                yield f"data: {_json.dumps({'text': chunk})}\n\n"
            if not first:
                # Cache the full assembled answer
                answer_cache.set(query_str, ("".join(chunks), label))
                yield "data: {\"done\": true}\n\n"
                return
        except Exception as e:
            log.debug("Stream answer [%s] error: %s", provider_key, e)

    yield "data: {\"done\": true}\n\n"


_MAX_TOKENS = 600   # enough for a full code example (~1-2 sentences + code block)


def _build_prompt(query: str, results: list, mode: str = "results") -> str:
    if mode == "free":
        return (
            f"Answer in 1-2 clear, factual sentences using your own knowledge. "
            f"Be concise.\n\nQuestion: {query}"
        )
    # Limit context to 2 snippets, 300 chars each to keep prompt small and fast
    context = "\n\n".join(
        f"Title: {r['title']}\n{(r.get('snippet') or '')[:300]}"
        for r in results[:2]
    )
    return (
        f"Answer in 1-2 sentences based only on these sources. "
        f"If they don't contain the answer, say so briefly.\n\n"
        f"Question: {query}\n\nSources:\n{context}"
    )


# ── Non-streaming providers ────────────────────────────────────────────────────

def _answer_ollama(prompt: str, cfg: dict) -> str | None:
    import requests as _req
    url   = (cfg.get("ollama_url") or "http://localhost:11434").rstrip("/")
    model = cfg.get("ollama_model") or "qwen2.5:0.5b"
    r = _req.post(
        f"{url}/api/chat",
        json={"model": model, "messages": [{"role": "user", "content": prompt}],
              "stream": False, "options": {"num_predict": _MAX_TOKENS}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def _answer_anthropic(prompt: str, cfg: dict) -> str | None:
    import os, anthropic
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    model = cfg.get("anthropic_model") or "claude-haiku-4-5-20251001"
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model, max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _answer_openai(prompt: str, cfg: dict) -> str | None:
    import os, requests as _req
    key = cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        return None
    model = cfg.get("openai_model") or "gpt-4o-mini"
    r = _req.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "max_tokens": _MAX_TOKENS,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _answer_gemini(prompt: str, cfg: dict) -> str | None:
    import os, requests as _req
    key = cfg.get("gemini_api_key") or os.environ.get("GOOGLE_API_KEY", "")
    if not key:
        return None
    model = cfg.get("gemini_model") or "gemini-1.5-flash"
    r = _req.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"maxOutputTokens": _MAX_TOKENS}},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


# ── Streaming providers (yield text chunks) ────────────────────────────────────

def _stream_ollama(prompt: str, cfg: dict):
    import requests as _req, json as _json
    url   = (cfg.get("ollama_url") or "http://localhost:11434").rstrip("/")
    model = cfg.get("ollama_model") or "qwen2.5:0.5b"
    r = _req.post(
        f"{url}/api/chat",
        json={"model": model, "messages": [{"role": "user", "content": prompt}],
              "stream": True, "options": {"num_predict": _MAX_TOKENS}},
        timeout=30, stream=True,
    )
    r.raise_for_status()
    for line in r.iter_lines():
        if not line:
            continue
        try:
            obj = _json.loads(line)
            chunk = obj.get("message", {}).get("content", "")
            if chunk:
                yield chunk
            if obj.get("done"):
                break
        except Exception:
            continue


def _stream_anthropic(prompt: str, cfg: dict):
    import os, anthropic
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return
    model  = cfg.get("anthropic_model") or "claude-haiku-4-5-20251001"
    client = anthropic.Anthropic(api_key=key)
    with client.messages.stream(
        model=model, max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for chunk in stream.text_stream:
            yield chunk


def _stream_openai(prompt: str, cfg: dict):
    import os, requests as _req, json as _json
    key = cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        return
    model = cfg.get("openai_model") or "gpt-4o-mini"
    r = _req.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "max_tokens": _MAX_TOKENS, "stream": True,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=15, stream=True,
    )
    r.raise_for_status()
    for line in r.iter_lines():
        if not line or line == b"data: [DONE]":
            continue
        raw = line.decode("utf-8").removeprefix("data: ")
        try:
            delta = _json.loads(raw)["choices"][0]["delta"].get("content", "")
            if delta:
                yield delta
        except Exception:
            continue


def _stream_gemini(prompt: str, cfg: dict):
    import os, requests as _req, json as _json
    key = cfg.get("gemini_api_key") or os.environ.get("GOOGLE_API_KEY", "")
    if not key:
        return
    model = cfg.get("gemini_model") or "gemini-1.5-flash"
    r = _req.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?key={key}&alt=sse",
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"maxOutputTokens": _MAX_TOKENS}},
        timeout=15, stream=True,
    )
    r.raise_for_status()
    for line in r.iter_lines():
        if not line:
            continue
        raw = line.decode("utf-8").removeprefix("data: ")
        try:
            obj   = _json.loads(raw)
            chunk = obj["candidates"][0]["content"]["parts"][0]["text"]
            if chunk:
                yield chunk
        except Exception:
            continue


_PROVIDERS = {
    "ollama":    _answer_ollama,
    "anthropic": _answer_anthropic,
    "openai":    _answer_openai,
    "gemini":    _answer_gemini,
}

_STREAM_PROVIDERS = {
    "ollama":    _stream_ollama,
    "anthropic": _stream_anthropic,
    "openai":    _stream_openai,
    "gemini":    _stream_gemini,
}

_PROVIDER_LABELS = {
    "ollama":    "Ollama",
    "anthropic": "Claude",
    "openai":    "OpenAI",
    "gemini":    "Gemini",
}


def get_answer_box(query: str, results: list, mode: str = "results") -> tuple[str | None, str | None]:
    """Returns (answer_text, provider_label) or (None, None)."""
    if not query:
        return None, None
    if mode == "results" and not results:
        return None, None
    try:
        import config as _cfg
        cfg = _cfg.load()
    except Exception:
        cfg = {}

    prompt   = _build_prompt(query, results, mode)
    selected = cfg.get("ai_provider", "ollama")
    # Try selected provider first, then fall through the rest
    order = [selected] + [p for p in _PROVIDERS if p != selected]

    for provider in order:
        fn = _PROVIDERS.get(provider)
        if not fn:
            continue
        try:
            text = fn(prompt, cfg)
            if text:
                return text, _PROVIDER_LABELS.get(provider, provider)
        except Exception as e:
            log.debug("Answer box [%s] error: %s", provider, e)

    return None, None


def suggestions(partial: str, limit: int = 8) -> list:
    if len(partial) < 2:
        return []
    conn = get_connection()
    rows = conn.execute(
        """SELECT DISTINCT title FROM documents
           WHERE title ILIKE %s
           LIMIT %s""",
        (f"%{partial}%", limit),
    ).fetchall()
    return [r["title"] for r in rows if r["title"]]
