import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULTS = {
    "open_allowed": [
        "mp4", "mkv", "avi", "mov", "webm", "m4v",
        "mp3", "wav", "flac", "ogg", "aac", "m4a",
        "jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg",
        "pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "txt", "csv"
    ],
    "open_denied": [
        "sh", "bash", "py", "js", "exe", "deb", "rpm",
        "bat", "cmd", "ps1", "rb", "pl", "php"
    ],
    "auto_crawl_enabled":        False,
    "auto_crawl_interval_hours": 24,
    "auto_crawl_last":           0,    # unix timestamp of last completed crawl

    # ── Crawler network settings ─────────────────────────────────────────────
    "crawler_max_concurrent":   500,   # global simultaneous HTTP requests
    "crawler_max_per_domain":   3,     # max concurrent requests to one domain
    "crawler_delay":            1.0,   # seconds between requests to same domain
    "crawler_db_workers":       32,    # ThreadPoolExecutor size for DB writes
    "crawler_connector_limit":  1000,  # aiohttp TCPConnector total connection cap
    "crawler_dns_ttl":          600,   # seconds to cache DNS lookups
    "crawler_domain_budget":    500,   # max pages crawled per domain per session
    "crawler_screenshots":      False, # capture JPEG screenshots after crawl

    # ── AI answer box ────────────────────────────────────────────────────────
    "ai_answer_mode":           "results",  # "results" = summarise indexed docs | "free" = AI own knowledge
    "ai_provider":              "ollama",   # ollama | anthropic | openai | gemini
    "anthropic_api_key":        "",
    "anthropic_model":          "claude-haiku-4-5-20251001",
    "openai_api_key":           "",
    "openai_model":             "gpt-4o-mini",
    "gemini_api_key":           "",
    "gemini_model":             "gemini-1.5-flash",
    "ollama_url":               "http://localhost:11434",
    "ollama_model":             "qwen2.5:0.5b",

    # ── TTS ──────────────────────────────────────────────────────────────────
    "tts_provider":             "edge",                   # edge | elevenlabs
    "edge_tts_voice":           "en-US-AriaNeural",       # free, no key needed
    "elevenlabs_api_key":       "",
    "elevenlabs_voice_id":      "21m00Tcm4TlvDq8ikWAM",  # Rachel (default)
    "elevenlabs_model":         "eleven_flash_v2_5",      # fastest model

    # ── Browser extension ────────────────────────────────────────────────────
    "extension_api_key":        "",    # optional key to protect /api/extension/add
}


def load() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return dict(DEFAULTS)


def save(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def is_open_allowed(path: str) -> tuple[bool, str]:
    ext = Path(path).suffix.lstrip(".").lower()
    cfg = load()

    if ext in [e.lower() for e in cfg.get("open_denied", [])]:
        return False, f".{ext} files are blocked from opening"

    allowed = [e.lower() for e in cfg.get("open_allowed", [])]
    if allowed and ext not in allowed:
        return False, f".{ext} is not in the allowed list"

    return True, ""
