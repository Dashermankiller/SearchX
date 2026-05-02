"""
Async web-discovery crawler — follows links across the entire web.

Improvements over the previous blocking version:
  - aiohttp + asyncio: 50 concurrent requests instead of 1
  - HEAD before GET: skips non-HTML content without downloading the body
  - trafilatura: extracts clean article text, discards nav/footer/ads
  - Thin content filter: skips pages with < 100 meaningful words
  - Canonical URL normalisation: strips tracking params before queuing
  - Link graph storage: every discovered edge saved for PageRank
  - Per-domain rate limiting and robots.txt compliance preserved
"""

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urljoin

import aiohttp
import trafilatura
from lxml import html as lxml_html
from lxml.etree import ParserError

from database import (
    get_connection, upsert_document,
    queue_urls, get_next_queued, mark_crawled,
    get_queue_stats, get_recent_crawled, get_pending_sample,
    store_links,
)
from url_utils import canonical
from simhash_util import compute as simhash_compute

log = logging.getLogger(__name__)

_USER_AGENT    = "SearchXBot/1.0 (local search engine; +http://localhost)"
_MAX_CONTENT   = 50_000   # chars to store
_THIN_WORDS    = 100      # pages below this word count are skipped
_ROBOTS_TTL    = 3600     # seconds to cache robots.txt per domain
_MAX_LINKS     = 300      # max outbound links to store per page


# ── Async robots.txt cache ──────────────────────────────────────────────────

class _RobotsCache:
    def __init__(self):
        self._cache: dict = {}   # domain -> (RobotFileParser|None, expires_at)
        self._lock  = asyncio.Lock()

    async def can_fetch(self, url: str, session: aiohttp.ClientSession) -> bool:
        from urllib.robotparser import RobotFileParser
        parsed     = urlparse(url)
        domain     = parsed.netloc
        robot_url  = f"{parsed.scheme}://{domain}/robots.txt"
        now        = asyncio.get_event_loop().time()

        async with self._lock:
            entry = self._cache.get(domain)
            if entry and now < entry[1]:
                rp = entry[0]
                return rp.can_fetch(_USER_AGENT, url) if rp else True

        rp = RobotFileParser()
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with session.get(robot_url, timeout=timeout) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="replace")
                    rp.parse(text.splitlines())
                else:
                    rp = None
        except Exception:
            rp = None

        async with self._lock:
            self._cache[domain] = (rp, now + _ROBOTS_TTL)

        return rp.can_fetch(_USER_AGENT, url) if rp else True


# ── Crawler daemon ──────────────────────────────────────────────────────────

def _net_cfg() -> dict:
    """Load crawler network settings from config, with fallback defaults."""
    try:
        import config as _cfg
        c = _cfg.load()
        return {
            "max_concurrent":   int(c.get("crawler_max_concurrent",  500)),
            "max_per_domain":   int(c.get("crawler_max_per_domain",  3)),
            "delay":            float(c.get("crawler_delay",          1.0)),
            "db_workers":       int(c.get("crawler_db_workers",       32)),
            "connector_limit":  int(c.get("crawler_connector_limit",  1000)),
            "dns_ttl":          int(c.get("crawler_dns_ttl",          600)),
            "domain_budget":    int(c.get("crawler_domain_budget",    500)),
            "screenshots":      bool(c.get("crawler_screenshots",     False)),
        }
    except Exception:
        return {
            "max_concurrent": 500, "max_per_domain": 3, "delay": 1.0,
            "db_workers": 32, "connector_limit": 1000, "dns_ttl": 600,
            "domain_budget": 500, "screenshots": False,
        }


# ── Sitemap discovery ────────────────────────────────────────────────────────

async def _parse_sitemap(url: str, session: aiohttp.ClientSession, depth: int = 0):
    """Parse a sitemap (or sitemap index) and return up to 5000 URLs."""
    if depth > 3:
        return []
    urls = []
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                return []
            raw = await resp.read()
    except Exception:
        return []

    try:
        from lxml import etree as lxml_etree
        root = lxml_etree.fromstring(raw)
        # Strip namespace for uniform XPath
        tag = root.tag
        ns = ""
        if "{" in tag:
            ns = tag.split("}")[0] + "}"

        # Sitemap index: contains <sitemap><loc>…
        sitemap_locs = root.findall(f".//{ns}sitemap/{ns}loc")
        if sitemap_locs:
            for loc_el in sitemap_locs:
                loc = (loc_el.text or "").strip()
                if loc:
                    sub = await _parse_sitemap(loc, session, depth + 1)
                    urls.extend(sub)
                    if len(urls) >= 5000:
                        break
        else:
            # Regular sitemap: <url><loc>…
            for loc_el in root.findall(f".//{ns}url/{ns}loc"):
                loc = (loc_el.text or "").strip()
                if loc:
                    urls.append(loc)
                    if len(urls) >= 5000:
                        break
    except Exception:
        pass

    return urls[:5000]


async def _discover_sitemaps(domain: str, session: aiohttp.ClientSession) -> list:
    """
    Discover sitemap URLs for a domain via robots.txt and /sitemap.xml.
    Returns a list of page URLs (up to 5000) found across all sitemaps.
    """
    sitemap_urls = []
    # 1. Try robots.txt for Sitemap: lines
    for scheme in ("https", "http"):
        robots_url = f"{scheme}://{domain}/robots.txt"
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with session.get(robots_url, timeout=timeout) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="replace")
                    for line in text.splitlines():
                        if line.lower().startswith("sitemap:"):
                            sm = line.split(":", 1)[1].strip()
                            if sm:
                                sitemap_urls.append(sm)
                    break
        except Exception:
            continue

    # 2. Fallback to /sitemap.xml
    if not sitemap_urls:
        for scheme in ("https", "http"):
            sitemap_urls.append(f"{scheme}://{domain}/sitemap.xml")
            break

    all_page_urls: list[str] = []
    seen_sitemaps: set[str] = set()
    for sm_url in sitemap_urls:
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)
        pages = await _parse_sitemap(sm_url, session)
        all_page_urls.extend(pages)
        if len(all_page_urls) >= 5000:
            break

    return all_page_urls[:5000]


# ── Feed indexing ────────────────────────────────────────────────────────────

def _index_feed(url: str, in_thread_fn=None):
    """
    Sync function: parse an RSS/Atom feed and upsert each entry as a document.
    Designed to run inside in_thread.
    """
    try:
        import feedparser
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title   = entry.get("title", "")
            link    = entry.get("link", "")
            summary = entry.get("summary", "") or entry.get("content", [{}])[0].get("value", "")
            if not link:
                continue
            # Parse published date
            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                import time as _time
                published = _time.strftime("%Y-%m-%dT%H:%M:%SZ", entry.published_parsed)
            upsert_document(
                url=link,
                title=title or link,
                content=summary[:50000] if summary else "",
                filetype="html",
                media_type="web",
                source="feed",
                metadata={"feed_url": url, "published": published},
            )
    except Exception as exc:
        log.debug("Feed indexing error for %s: %s", url, exc)


# ── YouTube transcript ───────────────────────────────────────────────────────

def _fetch_yt_transcript(url: str):
    """Sync: download YouTube auto-captions and return (title, transcript_text)."""
    import yt_dlp
    opts = {
        'skip_download': True,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en'],
        'subtitlesformat': 'vtt',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
    }
    info = {}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception:
            return None, None
    title = info.get('title', '')
    subtitles = info.get('automatic_captions') or info.get('subtitles') or {}
    en_subs = subtitles.get('en') or subtitles.get('en-US') or []
    transcript = ""
    for sub in en_subs:
        if sub.get('ext') in ('vtt', 'json3'):
            try:
                import urllib.request
                with urllib.request.urlopen(sub['url'], timeout=10) as r:
                    raw = r.read().decode('utf-8', errors='replace')
                lines = [l for l in raw.splitlines()
                         if l and not l.startswith('WEBVTT')
                         and '-->' not in l
                         and not l.strip().isdigit()]
                transcript = ' '.join(lines)[:50000]
                break
            except Exception:
                continue
    return title, transcript or info.get('description', '')


# ── Screenshot capture ───────────────────────────────────────────────────────

def _take_screenshot(url: str, path: str) -> bool:
    """Sync: capture a JPEG screenshot via sync_playwright. Returns True on success."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx  = browser.new_context(viewport={"width": 1280, "height": 800})
                page = ctx.new_page()
                page.goto(url, timeout=8000, wait_until="domcontentloaded")
                page.screenshot(path=path, type="jpeg", quality=85, full_page=False)
                return True
            finally:
                browser.close()
    except Exception as exc:
        log.debug("Screenshot failed for %s: %s", url, exc)
        return False


class _AsyncCrawlerDaemon:
    MAX_CONCURRENT  = 500   # global concurrent HTTP requests
    MAX_PER_DOMAIN  = 3     # concurrent requests to one domain (stay polite)
    DEFAULT_DELAY   = 1.0   # seconds between requests to the same domain

    def __init__(self):
        self._stop_flag   = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop:   asyncio.AbstractEventLoop | None = None
        self._delay   = self.DEFAULT_DELAY

        self._stat_lock       = threading.Lock()
        self._session_crawled = 0
        self._session_failed  = 0
        self._session_skipped = 0
        self._current_urls: set[str] = set()

    # ── Public API ───────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, seed_urls: list = None, delay: float = None) -> bool:
        if self.running:
            return False
        self._stop_flag.clear()
        if delay is not None:
            self._delay = max(0.1, float(delay))
        if seed_urls:
            # Canonicalise seeds before queuing
            clean = [c for u in seed_urls if (c := canonical(u))]
            queue_urls(clean or seed_urls, depth=0)
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="web-crawler"
        )
        self._thread.start()
        log.info("Async crawler started (delay=%.1fs, concurrency=%d)",
                 self._delay, self.MAX_CONCURRENT)
        return True

    def stop(self):
        self._stop_flag.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: None)  # wake the loop
        log.info("Crawler stop requested")

    def get_status(self) -> dict:
        db_stats = get_queue_stats()
        recent   = get_recent_crawled(limit=30)
        pending  = get_pending_sample(limit=15)
        with self._stat_lock:
            return {
                "running":         self.running,
                "current_urls":    list(self._current_urls),
                "current_url":     next(iter(self._current_urls), None),
                "session_crawled": self._session_crawled,
                "session_failed":  self._session_failed,
                "session_skipped": self._session_skipped,
                "delay":           self._delay,
                **db_stats,
                "recent":          recent,
                "pending_sample":  pending,
            }

    # ── Thread entry ─────────────────────────────────────────────────────────

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        finally:
            loop.close()
            self._loop = None

    # ── Async main loop ───────────────────────────────────────────────────────

    async def _async_main(self):
        stop_event = asyncio.Event()

        # Bridge threading.Event → asyncio.Event
        async def _watch():
            while not self._stop_flag.is_set():
                await asyncio.sleep(0.3)
            stop_event.set()
        asyncio.create_task(_watch())

        # Load network settings from config at crawl start
        net = _net_cfg()
        max_concurrent = net["max_concurrent"]

        executor = ThreadPoolExecutor(max_workers=net["db_workers"], thread_name_prefix="crawldb")

        async def in_thread(fn, *args, **kwargs):
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(executor, lambda: fn(*args, **kwargs))

        global_sem    = asyncio.Semaphore(max_concurrent)
        domain_sems:  dict[str, asyncio.Semaphore] = {}
        domain_times: dict[str, float] = {}
        domain_counts: dict[str, int] = {}
        robots        = _RobotsCache()
        _feed_urls:   set[str] = set()
        _visited_domains: set[str] = set()

        connector = aiohttp.TCPConnector(
            limit=net["connector_limit"],
            limit_per_host=net["max_per_domain"] + 2,
            ttl_dns_cache=net["dns_ttl"],
            ssl=False,
        )
        timeout = aiohttp.ClientTimeout(total=20, connect=5, sock_read=15)

        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
        ) as session:
            active: set[asyncio.Task] = set()
            idle_rounds = 0

            while not stop_event.is_set():
                # Fill up to max_concurrent tasks
                while len(active) < max_concurrent and not stop_event.is_set():
                    row = await in_thread(get_next_queued)
                    if not row:
                        break
                    idle_rounds = 0
                    t = asyncio.create_task(
                        self._crawl_one(
                            session, global_sem, domain_sems, domain_times,
                            domain_counts, robots, row, in_thread, net,
                            _feed_urls, _visited_domains,
                        )
                    )
                    active.add(t)
                    t.add_done_callback(active.discard)

                if not active:
                    idle_rounds += 1
                    wait = min(5 * idle_rounds, 60)
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(stop_event.wait()), timeout=wait
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue

                # Wait for at least one task to finish before refilling
                await asyncio.wait(active, timeout=1.0,
                                   return_when=asyncio.FIRST_COMPLETED)

        # ── Index discovered feeds after main crawl ───────────────────────────
        if _feed_urls:
            log.info("Indexing %d discovered feed(s)…", len(_feed_urls))
            feed_tasks = [
                asyncio.create_task(in_thread(_index_feed, fu))
                for fu in list(_feed_urls)
            ]
            if feed_tasks:
                await asyncio.gather(*feed_tasks, return_exceptions=True)

        # Graceful shutdown
        for t in active:
            t.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        executor.shutdown(wait=False)
        log.info("Crawler stopped. session: crawled=%d failed=%d skipped=%d",
                 self._session_crawled, self._session_failed, self._session_skipped)

    # ── Single URL crawl ──────────────────────────────────────────────────────

    async def _crawl_one(self, session, global_sem, domain_sems, domain_times,
                         domain_counts, robots, row, in_thread, net,
                         feed_urls_set, visited_domains):
        url    = row["url"]
        depth  = row["depth"]
        parsed = urlparse(url)
        domain = parsed.netloc

        with self._stat_lock:
            self._current_urls.add(url)

        try:
            # ── YouTube transcript shortcut ──────────────────────────────────
            if 'youtube.com/watch' in url or 'youtu.be/' in url:
                title, transcript = await in_thread(_fetch_yt_transcript, url)
                if transcript:
                    await in_thread(
                        upsert_document,
                        url=url, title=title or url,
                        content=transcript[:_MAX_CONTENT],
                        filetype="html", media_type="web",
                        source="webcrawler",
                        metadata={"domain": domain, "youtube": True},
                    )
                    await in_thread(mark_crawled, url, True)
                    with self._stat_lock:
                        self._session_crawled += 1
                else:
                    await in_thread(mark_crawled, url, False, "yt: no transcript")
                    with self._stat_lock:
                        self._session_skipped += 1
                return

            # ── Per-domain budget check ──────────────────────────────────────
            if domain_counts.get(domain, 0) >= net["domain_budget"]:
                await in_thread(mark_crawled, url, False, "domain budget exceeded")
                with self._stat_lock:
                    self._session_skipped += 1
                return

            async with global_sem:
                # Per-domain semaphore (sized from config)
                if domain not in domain_sems:
                    domain_sems[domain] = asyncio.Semaphore(net["max_per_domain"])

                async with domain_sems[domain]:
                    # Sitemap discovery on first visit to a domain
                    if domain not in visited_domains:
                        visited_domains.add(domain)
                        try:
                            sitemap_urls = await _discover_sitemaps(domain, session)
                            if sitemap_urls:
                                canon_sm = [c for u in sitemap_urls if (c := canonical(u))]
                                await in_thread(queue_urls, canon_sm, depth + 1)
                                log.debug("Sitemap: queued %d URLs for %s",
                                          len(canon_sm), domain)
                        except Exception as exc:
                            log.debug("Sitemap discovery failed for %s: %s", domain, exc)

                    # Per-domain rate limit (from config)
                    now  = asyncio.get_event_loop().time()
                    wait = net["delay"] - (now - domain_times.get(domain, 0))
                    if wait > 0:
                        await asyncio.sleep(wait)
                    domain_times[domain] = asyncio.get_event_loop().time()

                    # ── robots.txt ──────────────────────────────────────────
                    allowed = await robots.can_fetch(url, session)
                    if not allowed:
                        await in_thread(mark_crawled, url, False, "blocked by robots.txt")
                        with self._stat_lock:
                            self._session_skipped += 1
                        return

                    # ── HEAD request — skip non-HTML without downloading ────
                    final_url = url
                    try:
                        async with session.head(url, allow_redirects=True) as head:
                            ct = head.headers.get("content-type", "")
                            if "text/html" not in ct and ct != "":
                                await in_thread(mark_crawled, url, True)
                                return
                            final_url = str(head.url)
                    except aiohttp.ClientResponseError as e:
                        if e.status == 405:
                            pass   # server doesn't support HEAD — try GET anyway
                        else:
                            await in_thread(mark_crawled, url, False, f"HEAD {e.status}")
                            with self._stat_lock:
                                self._session_failed += 1
                            return
                    except Exception:
                        pass  # fallback to GET

                    # ── GET full page ────────────────────────────────────────
                    try:
                        async with session.get(final_url, allow_redirects=True) as resp:
                            resp.raise_for_status()
                            ct = resp.headers.get("content-type", "")
                            if "text/html" not in ct:
                                await in_thread(mark_crawled, url, True)
                                return
                            html = await resp.text(errors="replace")
                            final_url = str(resp.url)
                    except aiohttp.ClientResponseError as e:
                        await in_thread(mark_crawled, url, False, f"HTTP {e.status}")
                        with self._stat_lock:
                            self._session_failed += 1
                        return
                    except Exception as exc:
                        await in_thread(mark_crawled, url, False, str(exc)[:200])
                        with self._stat_lock:
                            self._session_failed += 1
                        return

            # ── Content extraction (outside HTTP lock) ───────────────────────

            # trafilatura for clean article text
            content = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                no_fallback=False,
            )

            # Thin content filter
            if not content or len(content.split()) < _THIN_WORDS:
                await in_thread(mark_crawled, url, False, "thin content")
                with self._stat_lock:
                    self._session_skipped += 1
                return

            content = content[:_MAX_CONTENT]

            # ── Metadata + link extraction via lxml (5-10x faster than BS4) ─
            try:
                tree = lxml_html.fromstring(html, base_url=final_url)
                tree.make_links_absolute(final_url, resolve_base_href=True)
            except (ParserError, ValueError):
                await in_thread(mark_crawled, url, False, "lxml parse error")
                with self._stat_lock:
                    self._session_failed += 1
                return

            # Title
            title_els = tree.xpath("//title/text()")
            title     = title_els[0].strip() if title_els else domain

            # ── RSS/Atom feed detection ──────────────────────────────────────
            for feed_href in tree.xpath(
                '//link[@rel="alternate"]'
                '[@type="application/rss+xml" or @type="application/atom+xml"]/@href'
            ):
                if feed_href:
                    feed_urls_set.add(feed_href)

            # Meta description
            desc_els    = tree.xpath('//meta[translate(@name,"DESCRIPTION","description")="description"]/@content')
            description = desc_els[0][:500] if desc_els else ""

            # Links — lxml already resolved to absolute, just canonicalise
            seen_links: set[str] = set()
            links: list[str] = []
            for href in tree.xpath("//a/@href"):
                if not href:
                    continue
                canon = canonical(href)
                if canon and canon not in seen_links:
                    seen_links.add(canon)
                    links.append(canon)
                if len(links) >= _MAX_LINKS:
                    break

            # Images — store up to 30 absolute URLs (skip data URIs and trackers)
            seen_imgs: set[str] = set()
            images: list[dict] = []
            for el in tree.xpath("//img"):
                src = el.get("src", "")
                if not src or src.startswith("data:") or src in seen_imgs:
                    continue
                # Skip tiny tracker pixels (1x1)
                w = el.get("width", "")
                h = el.get("height", "")
                if w == "1" or h == "1":
                    continue
                seen_imgs.add(src)
                images.append({
                    "src": src,
                    "alt": (el.get("alt") or "")[:120],
                    "w":   w,
                    "h":   h,
                })
                if len(images) >= 30:
                    break

            # Videos — src from <video> or <video><source>
            seen_vids: set[str] = set()
            videos: list[dict] = []
            for el in tree.xpath("//video"):
                poster = el.get("poster", "")
                srcs = ([el.get("src")] if el.get("src") else []) + \
                       [s.get("src", "") for s in el.xpath("source") if s.get("src")]
                for src in srcs:
                    if src and not src.startswith("data:") and src not in seen_vids:
                        seen_vids.add(src)
                        videos.append({
                            "src":    src,
                            "poster": poster,
                            "type":   el.get("type", ""),
                        })
                if len(videos) >= 10:
                    break

            # ── Near-duplicate check ─────────────────────────────────────────
            from database import is_near_duplicate
            sh     = simhash_compute(content)
            is_dup = await in_thread(is_near_duplicate, sh, final_url)
            if is_dup:
                await in_thread(mark_crawled, url, False, "near-duplicate")
                with self._stat_lock:
                    self._session_skipped += 1
                return

            # ── Index the page ───────────────────────────────────────────────
            await in_thread(
                upsert_document,
                url        = final_url,
                title      = title,
                content    = content,
                filetype   = "html",
                media_type = "web",
                source     = "webcrawler",
                metadata   = {
                    "description": description,
                    "depth":       depth,
                    "domain":      domain,
                    "images":      images,
                    "videos":      videos,
                },
            )
            await in_thread(get_connection().commit)

            # ── Store link graph + enqueue discovered URLs ───────────────────
            if links:
                await in_thread(store_links, final_url, links)
                await in_thread(queue_urls, links, depth + 1)

            await in_thread(mark_crawled, url, True)
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            with self._stat_lock:
                self._session_crawled += 1

            # ── Optional screenshot ──────────────────────────────────────────
            if net.get("screenshots"):
                try:
                    import hashlib
                    from pathlib import Path as _Path
                    thumb_dir = _Path(__file__).parent / "static" / "thumbs"
                    thumb_dir.mkdir(parents=True, exist_ok=True)
                    md5hex = hashlib.md5(final_url.encode()).hexdigest()[:12]
                    thumb_path = str(thumb_dir / f"shot_{md5hex}.jpg")
                    ok = await asyncio.to_thread(_take_screenshot, final_url, thumb_path)
                    if ok:
                        rel_path = f"thumbs/shot_{md5hex}.jpg"
                        await in_thread(
                            upsert_document,
                            url=final_url, title=title,
                            content=content[:_MAX_CONTENT],
                            filetype="html", media_type="web",
                            source="webcrawler",
                            metadata={
                                "description": description,
                                "depth": depth,
                                "domain": domain,
                                "images": images,
                                "videos": videos,
                                "thumb_path": rel_path,
                            },
                        )
                except Exception as exc:
                    log.debug("Screenshot post-process error: %s", exc)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Unexpected crawl error on %s: %s", url, exc)
            try:
                await in_thread(mark_crawled, url, False, str(exc)[:200])
            except Exception:
                pass
            with self._stat_lock:
                self._session_failed += 1
        finally:
            with self._stat_lock:
                self._current_urls.discard(url)


# ── Module-level singleton ──────────────────────────────────────────────────

_daemon = _AsyncCrawlerDaemon()


def start_crawler(seed_urls: list = None, delay: float = None) -> bool:
    return _daemon.start(seed_urls=seed_urls, delay=delay)


def stop_crawler():
    _daemon.stop()


def get_crawler_status() -> dict:
    return _daemon.get_status()
