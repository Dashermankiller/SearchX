import hashlib
import json
import os
import re
import threading
import time
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from database import upsert_document, upsert_source, finish_source, rebuild_fts, get_document_mtime, commit
from security import is_safe_url

try:
    from PIL import Image, ExifTags
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pytesseract
    HAS_OCR = True
except ImportError:
    HAS_OCR = False

try:
    import mutagen
    from mutagen.mp4 import MP4
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

try:
    import pdfplumber
    HAS_PDF = True
except ImportError:
    HAS_PDF = False

try:
    from docx import Document as DocxDocument
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

log = logging.getLogger(__name__)

THUMB_DIR = Path(__file__).parent / "static" / "thumbs"
_STATUS_FILE = Path(__file__).parent / ".index_status.json"

MEDIA_TYPE_MAP = {
    "image":    {"jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "tiff"},
    "audio":    {"mp3", "wav", "flac", "ogg", "aac", "m4a"},
    "video":    {"mp4", "mkv", "avi", "mov", "webm", "m4v"},
    "document": {"pdf", "docx", "doc", "txt", "xlsx", "xls", "pptx", "ppt", "csv"},
    "web":      {"html", "htm"},
}

_ext_to_media = {ext: mt for mt, exts in MEDIA_TYPE_MAP.items() for ext in exts}


def _ext(path: str) -> str:
    return Path(path).suffix.lstrip(".").lower()


def _media_type(ext: str) -> str:
    return _ext_to_media.get(ext, "other")


# ---------------------------------------------------------------------------
# File utilities
# ---------------------------------------------------------------------------

def _file_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except Exception:
        return 0.0


def _file_hash(path: Path, max_bytes: int = 10 * 1024 * 1024) -> str:
    """SHA256 of file content (first+last 4KB for large files)."""
    try:
        size = path.stat().st_size
        h = hashlib.sha256()
        with open(path, "rb") as f:
            if size <= max_bytes:
                h.update(f.read())
            else:
                h.update(f.read(4096))
                f.seek(-4096, 2)
                h.update(f.read(4096))
        return h.hexdigest()
    except Exception:
        return ""


def _generate_thumb(path: Path, url: str) -> str | None:
    """Generate a 200×200 JPEG thumbnail. Returns 'thumbs/<name>.jpg' or None."""
    if not HAS_PIL:
        return None
    try:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        fname = hashlib.md5(url.encode()).hexdigest() + ".jpg"
        thumb_path = THUMB_DIR / fname
        if thumb_path.exists():
            return f"thumbs/{fname}"
        with Image.open(path) as img:
            img.thumbnail((200, 200), Image.LANCZOS)
            rgb = img.convert("RGB")
            rgb.save(thumb_path, "JPEG", quality=78, optimize=True)
        return f"thumbs/{fname}"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Metadata extractors
# ---------------------------------------------------------------------------

def _extract_image(path: Path) -> tuple:
    meta = {}
    ocr_text = ""
    if HAS_PIL:
        try:
            with Image.open(path) as img:
                meta["width"], meta["height"] = img.size
                meta["format"] = img.format
                exif = img._getexif() if hasattr(img, "_getexif") else None
                if exif:
                    for tag_id, value in exif.items():
                        tag = ExifTags.TAGS.get(tag_id, tag_id)
                        if isinstance(value, str):
                            meta[str(tag)] = value[:200]
                if HAS_OCR:
                    try:
                        ocr_text = pytesseract.image_to_string(img)[:3000]
                    except Exception:
                        pass
        except Exception:
            pass
    title = path.stem.replace("_", " ").replace("-", " ").title()
    return title, ocr_text, meta


def _extract_audio(path: Path) -> tuple:
    meta = {}
    title = path.stem
    if HAS_MUTAGEN:
        try:
            audio = mutagen.File(path, easy=True)
            if audio:
                for key in ("title", "artist", "album", "date", "genre"):
                    val = audio.get(key)
                    if val:
                        meta[key] = str(val[0])
                title = meta.get("title", title)
                if hasattr(audio, "info"):
                    meta["duration_sec"] = round(getattr(audio.info, "length", 0))
                    meta["bitrate"] = getattr(audio.info, "bitrate", None)
        except Exception:
            pass
    content = " ".join(str(v) for v in meta.values())
    return title, content, meta


def _extract_video(path: Path) -> tuple:
    meta = {}
    title = path.stem
    if HAS_MUTAGEN:
        try:
            video = mutagen.File(path)
            if video and hasattr(video, "info"):
                meta["duration_sec"] = round(getattr(video.info, "length", 0))
            if isinstance(video, MP4):
                tags = video.tags or {}
                if "\xa9nam" in tags:
                    title = str(tags["\xa9nam"][0])
                    meta["title"] = title
        except Exception:
            pass
    content = " ".join(str(v) for v in meta.values())
    return title, content, meta


def _extract_pdf(path: Path) -> tuple:
    title = path.stem
    content = ""
    meta = {}
    if HAS_PDF:
        try:
            with pdfplumber.open(path) as pdf:
                meta["pages"] = len(pdf.pages)
                if pdf.metadata:
                    if pdf.metadata.get("Title"):
                        title = pdf.metadata["Title"]
                    meta.update({k: str(v)[:200] for k, v in pdf.metadata.items() if v})
                texts = []
                for page in pdf.pages[:20]:
                    t = page.extract_text()
                    if t:
                        texts.append(t)
                content = "\n".join(texts)[:8000]
        except Exception:
            pass
    return title, content, meta


def _extract_docx(path: Path) -> tuple:
    title = path.stem
    content = ""
    meta = {}
    if HAS_DOCX:
        try:
            doc = DocxDocument(str(path))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            if paragraphs:
                title = paragraphs[0][:120]
            content = "\n".join(paragraphs)[:8000]
            core = doc.core_properties
            meta["author"] = str(getattr(core, "author", "") or "")
        except Exception:
            pass
    return title, content, meta


def _extract_text(path: Path) -> tuple:
    title = path.stem
    content = ""
    try:
        content = path.read_text(errors="replace")[:8000]
        if content:
            title = content.split("\n", 1)[0][:120].strip() or title
    except Exception:
        pass
    return title, content, {}


EXTRACTORS = {
    "image":    _extract_image,
    "audio":    _extract_audio,
    "video":    _extract_video,
    "document": {
        "pdf":  _extract_pdf,
        "docx": _extract_docx,
        "doc":  _extract_docx,
        "txt":  _extract_text,
        "csv":  _extract_text,
    },
}


def _extract(path: Path, ext: str, mt: str):
    if mt == "document":
        extractor = EXTRACTORS["document"].get(ext, _extract_text)
    else:
        extractor = EXTRACTORS.get(mt)
    if extractor:
        return extractor(path)
    return path.stem, "", {}


# ---------------------------------------------------------------------------
# Local indexer
# ---------------------------------------------------------------------------

class LocalIndexer:
    def __init__(self, paths: list, extensions: set | None = None, settings: dict | None = None):
        self.paths = [Path(p) for p in paths]
        self.extensions = extensions
        self.settings = settings or {}

    def index(self, progress_cb=None):
        all_exts = set(_ext_to_media.keys())
        allowed = self.extensions or all_exts
        count = 0

        for root_path in self.paths:
            if not root_path.exists():
                log.warning("Path does not exist: %s", root_path)
                continue

            source_id = upsert_source(
                "local", str(root_path),
                label=str(root_path),
                settings=self.settings,
            )

            for dirpath, _, files in os.walk(root_path):
                for fname in files:
                    fpath = Path(dirpath) / fname
                    ext = _ext(fpath.name)
                    if ext not in allowed:
                        continue
                    mt = _media_type(ext)
                    try:
                        stat = fpath.stat()
                        mtime = stat.st_mtime
                        size = stat.st_size
                        url = fpath.as_uri()

                        # Skip unchanged files (incremental indexing)
                        stored_mtime = get_document_mtime(url)
                        if stored_mtime is not None and abs(stored_mtime - mtime) < 1.0:
                            count += 1
                            if progress_cb:
                                progress_cb(count, str(fpath))
                            continue

                        title, content, meta = _extract(fpath, ext, mt)

                        # Thumbnail for images
                        thumb_path = None
                        if mt == "image":
                            thumb_path = _generate_thumb(fpath, url)

                        # Hash for dedup (skip very large files)
                        file_hash = _file_hash(fpath) if size < 200 * 1024 * 1024 else ""

                        upsert_document(
                            url=url,
                            title=title,
                            content=content,
                            filetype=ext,
                            media_type=mt,
                            source="local",
                            file_path=str(fpath),
                            file_size=size,
                            metadata=meta,
                            source_id=source_id,
                            mtime=mtime,
                            file_hash=file_hash,
                            thumb_path=thumb_path,
                        )
                        count += 1
                        if count % 100 == 0:
                            commit()  # batch commit every 100 files
                        if progress_cb:
                            progress_cb(count, str(fpath))
                    except Exception as e:
                        log.error("Error indexing %s: %s", fpath, e)

            commit()  # final commit for the last batch
            finish_source(source_id)
        return count


# ---------------------------------------------------------------------------
# Web crawler
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":  "document",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-Site":  "none",
    "Sec-Fetch-User":  "?1",
    "DNT":             "1",
}

# Fallback UA tried when a site returns 403/429 on the first attempt
_FALLBACK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4.1 Safari/605.1.15"
)

_SKIP_EXTS = {
    "js", "css", "woff", "woff2", "ttf", "eot", "ico",
    "gz", "zip", "tar", "exe", "dmg", "apk",
}


class WebCrawler:
    def __init__(
        self,
        start_urls: list,
        max_depth: int = 2,
        max_pages: int = 500,
        same_domain: bool = True,
        delay: float = 0.5,
        settings: dict | None = None,
    ):
        self.start_urls = start_urls
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.same_domain = same_domain
        self.delay = delay
        self.settings = settings or {}
        self._visited: set = set()
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        self._session.verify = False
        self._source_ids: dict = {}

    def _allowed_domain(self, url: str, base: str) -> bool:
        if not self.same_domain:
            return True
        return urlparse(url).netloc == urlparse(base).netloc

    def _fetch(self, url: str):
        try:
            r = self._session.get(url, timeout=10, allow_redirects=True)
            if r.status_code in (403, 429, 503):
                self._session.headers.update({"User-Agent": _FALLBACK_UA})
                r = self._session.get(url, timeout=10, allow_redirects=True)
                self._session.headers.update({"User-Agent": HEADERS["User-Agent"]})
            r.raise_for_status()
            return r
        except Exception as e:
            log.debug("Fetch failed %s: %s", url, e)
            return None

    def _source_id_for(self, base: str) -> int:
        if base not in self._source_ids:
            sid = upsert_source("web", base, label=base, settings=self.settings)
            self._source_ids[base] = sid
        return self._source_ids[base]

    def _index_page(self, url: str, html: str, source_id: int):
        soup = BeautifulSoup(html, "lxml")
        title = (soup.title.string or "").strip()[:200] if soup.title else url

        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"].strip()[:200]

        desc = ""
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            desc = meta_desc["content"].strip()[:500]

        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        body_text = soup.get_text(separator=" ", strip=True)[:8000]
        content = f"{desc}\n{body_text}".strip()

        meta = {"description": desc}
        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            meta["og_image"] = og_image["content"]

        upsert_document(
            url=url, title=title, content=content,
            filetype="html", media_type="web", source="web",
            metadata=meta, source_id=source_id,
        )

    def _index_media(self, url: str, ext: str, source_id: int):
        mt = _media_type(ext)
        fname = Path(urlparse(url).path).name
        title = Path(fname).stem.replace("_", " ").replace("-", " ").title()
        upsert_document(
            url=url, title=title, content="",
            filetype=ext, media_type=mt, source="web",
            metadata={}, source_id=source_id,
        )

    def _crawl(self, url: str, depth: int, base: str, progress_cb=None):
        if depth < 0 or url in self._visited or len(self._visited) >= self.max_pages:
            return
        self._visited.add(url)

        ext = _ext(urlparse(url).path)
        if ext in _SKIP_EXTS:
            return

        sid = self._source_id_for(base)

        if ext and ext in _ext_to_media and ext not in {"html", "htm"}:
            self._index_media(url, ext, sid)
            if len(self._visited) % 50 == 0:
                commit()
            if progress_cb:
                progress_cb(len(self._visited), url)
            return

        resp = self._fetch(url)
        if resp is None:
            return

        ct = resp.headers.get("content-type", "")
        if "html" not in ct:
            detected_ext = ext or ct.split("/")[-1].split(";")[0].strip()
            if detected_ext in _ext_to_media:
                self._index_media(url, detected_ext, sid)
            return

        self._index_page(url, resp.text, sid)
        if len(self._visited) % 50 == 0:
            commit()
        if progress_cb:
            progress_cb(len(self._visited), url)

        if depth == 0:
            return

        soup = BeautifulSoup(resp.text, "lxml")
        links = set()
        for tag in soup.find_all("a", href=True):
            href = urljoin(url, tag["href"]).split("#")[0]
            parsed = urlparse(href)
            if parsed.scheme not in ("http", "https"):
                continue
            if not self._allowed_domain(href, base):
                continue
            links.add(href)

        time.sleep(self.delay)
        for link in links:
            if is_safe_url(link)[0]:
                self._crawl(link, depth - 1, base, progress_cb)

    def crawl(self, progress_cb=None):
        for start in self.start_urls:
            ok, reason = is_safe_url(start)
            if not ok:
                log.warning("Blocked unsafe URL %s: %s", start, reason)
                continue
            self._crawl(start, self.max_depth, start, progress_cb)
        for base, sid in self._source_ids.items():
            finish_source(sid)
        return len(self._visited)


# ---------------------------------------------------------------------------
# Background index job — status shared across Gunicorn workers via a file
# ---------------------------------------------------------------------------

_DEFAULT_STATUS = {"running": False, "count": 0, "current": "", "done": False, "error": ""}


def _write_status(status: dict):
    """Atomic write so readers never see a partial file."""
    tmp = _STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status))
    tmp.replace(_STATUS_FILE)


def get_index_status() -> dict:
    try:
        return json.loads(_STATUS_FILE.read_text())
    except Exception:
        return dict(_DEFAULT_STATUS)


def reindex_source(source_id: int, src: dict):
    s = src.get("settings", {})
    if src["type"] == "local":
        exts = set(s.get("extensions", [])) or None
        run_index_job([src["path"]], [], {"extensions": exts})
    else:
        run_index_job([], [src["path"]], {
            "depth":       s.get("depth", 2),
            "max_pages":   s.get("max_pages", 200),
            "same_domain": s.get("same_domain", True),
            "delay":       s.get("delay", 0.3),
        })


def index_single_url(url: str) -> dict:
    """
    Fetch and index exactly one URL — no link-following, no crawling.
    Returns {"ok": True, "title": ...} or {"ok": False, "error": ...}.
    """
    # Normalise: add https:// if missing
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    session = requests.Session()
    session.headers.update(HEADERS)
    session.verify = False

    r        = None
    html_pw  = None   # set if Playwright fallback was used
    final_url = url

    try:
        r = session.get(url, timeout=15, allow_redirects=True)
        if r.status_code in (403, 429, 503):
            session.headers.update({"User-Agent": _FALLBACK_UA})
            r = session.get(url, timeout=15, allow_redirects=True)
            session.headers.update({"User-Agent": HEADERS["User-Agent"]})
        if r.status_code in (403, 429, 503):
            raise requests.HTTPError(response=r)
        r.raise_for_status()
        final_url = r.url
    except (requests.HTTPError, requests.ConnectionError):
        # Plain HTTP failed — try Playwright (handles Cloudflare JS challenges)
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx     = browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    locale="en-US",
                    viewport={"width": 1280, "height": 800},
                )
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                # Wait a moment for JS challenges to resolve
                page.wait_for_timeout(2500)
                final_url = page.url
                html_pw   = page.content()
                browser.close()
        except Exception as e:
            return {"ok": False, "error": f"Blocked by site and Playwright fallback failed: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    ct  = (r.headers.get("content-type", "") if r else "text/html").lower()
    ext = _ext(final_url)

    source_id  = upsert_source("web", final_url, label=final_url)

    if "html" in ct or ext in {"html", "htm", ""} or html_pw:
        raw_html = html_pw if html_pw else r.text
        soup   = BeautifulSoup(raw_html, "lxml")
        title  = (soup.title.string or "").strip()[:200] if soup.title else final_url
        og     = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"].strip()[:200]
        meta_d = soup.find("meta", attrs={"name": "description"})
        desc   = (meta_d["content"].strip()[:500]) if meta_d and meta_d.get("content") else ""
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        body   = soup.get_text(separator=" ", strip=True)[:8000]
        content = f"{desc}\n{body}".strip()
        upsert_document(
            url=final_url, title=title, content=content,
            filetype="html", media_type="web", source="web",
            metadata={"description": desc}, source_id=source_id,
        )
    else:
        # Non-HTML resource (image, pdf, etc.) — index as media
        mt    = _media_type(ext) or "other"
        title = final_url.split("/")[-1] or final_url
        upsert_document(
            url=final_url, title=title, content="",
            filetype=ext, media_type=mt, source="web",
            metadata={}, source_id=source_id,
        )
        title = final_url

    rebuild_fts()
    commit()
    return {"ok": True, "title": title, "url": final_url}


def run_index_job(local_paths: list, web_urls: list, options: dict):
    def _run():
        _write_status({"running": True, "count": 0, "current": "", "done": False, "error": ""})
        try:
            def cb(n, item):
                _write_status({"running": True, "count": n, "current": item, "done": False, "error": ""})

            if local_paths:
                exts = options.get("extensions") or None
                li = LocalIndexer(local_paths, extensions=exts)
                li.index(progress_cb=cb)

            if web_urls:
                wc = WebCrawler(
                    start_urls=web_urls,
                    max_depth=options.get("depth", 2),
                    max_pages=options.get("max_pages", 200),
                    same_domain=options.get("same_domain", True),
                    delay=options.get("delay", 0.3),
                )
                wc.crawl(progress_cb=cb)

            rebuild_fts()
            _write_status({"running": False, "count": get_index_status().get("count", 0),
                           "current": "", "done": True, "error": ""})
        except Exception as e:
            _write_status({"running": False, "count": 0, "current": "", "done": False, "error": str(e)})

    t = threading.Thread(target=_run, daemon=True)
    t.start()
