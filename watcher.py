import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_observer = None
_watching = False
_lock = threading.Lock()

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

MEDIA_EXTS = {
    "jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "tiff",
    "mp3", "wav", "flac", "ogg", "aac", "m4a",
    "mp4", "mkv", "avi", "mov", "webm", "m4v",
    "pdf", "docx", "doc", "txt", "xlsx", "xls", "pptx", "ppt", "csv",
    "html", "htm",
}


class _MediaHandler(FileSystemEventHandler):
    def __init__(self, source_id: int, source_path: str):
        self.source_id = source_id
        self.source_path = source_path

    def _ext(self, path: str) -> str:
        return Path(path).suffix.lstrip(".").lower()

    def on_created(self, event):
        if not event.is_directory and self._ext(event.src_path) in MEDIA_EXTS:
            self._reindex()

    def on_modified(self, event):
        if not event.is_directory and self._ext(event.src_path) in MEDIA_EXTS:
            self._reindex()

    def on_deleted(self, event):
        if not event.is_directory:
            try:
                from database import get_connection
                url = Path(event.src_path).as_uri()
                with get_connection() as conn:
                    conn.execute("DELETE FROM documents WHERE url = %s", (url,))
                log.info("Watcher: removed %s from index", event.src_path)
            except Exception as e:
                log.error("Watcher delete error: %s", e)

    def _reindex(self):
        from indexer import run_index_job
        run_index_job([self.source_path], [], {})


def start_watcher(sources: list) -> bool:
    global _observer, _watching
    if not HAS_WATCHDOG:
        log.warning("watchdog not installed — file watcher unavailable")
        return False
    with _lock:
        if _observer and _observer.is_alive():
            return True
        obs = Observer()
        watched = 0
        for src in sources:
            if src.get("type") == "local" and src.get("enabled") and Path(src["path"]).is_dir():
                obs.schedule(_MediaHandler(src["id"], src["path"]), src["path"], recursive=True)
                watched += 1
        if not watched:
            return False
        obs.start()
        _observer = obs
        _watching = True
        log.info("File watcher started on %d sources", watched)
        return True


def stop_watcher():
    global _observer, _watching
    with _lock:
        if _observer:
            _observer.stop()
            _observer.join(timeout=5)
            _observer = None
        _watching = False


def is_watching() -> bool:
    return _watching and _observer is not None and _observer.is_alive()
