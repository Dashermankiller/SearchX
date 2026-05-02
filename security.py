import html
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Path validation — only serve files that are in the indexed documents table
# ---------------------------------------------------------------------------

def validate_indexed_path(path: str) -> tuple[bool, str]:
    """Return (ok, error). Path must resolve to a file that exists in the DB."""
    if not path:
        return False, "No path provided"

    try:
        resolved = str(Path(path).resolve())
    except Exception:
        return False, "Invalid path"

    if not Path(resolved).is_file():
        return False, "File not found"

    # Only serve files that were explicitly indexed
    from database import get_connection
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE file_path = %s AND source = 'local'",
            (resolved,),
        ).fetchone()
    if not row:
        return False, "File is not in the search index"

    return True, resolved


# ---------------------------------------------------------------------------
# SSRF protection — block private / loopback addresses in crawler URLs
# ---------------------------------------------------------------------------

_BLOCKED_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / AWS metadata endpoint
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def is_safe_url(url: str) -> tuple[bool, str]:
    """Return (ok, reason). Rejects non-http(s), loopback, and RFC-1918 targets."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Malformed URL"

    if parsed.scheme not in ("http", "https"):
        return False, f"Scheme '{parsed.scheme}' not allowed — only http/https"

    host = parsed.hostname or ""
    if not host:
        return False, "No host in URL"

    if host.lower() in _BLOCKED_HOSTS:
        return False, f"Host '{host}' is blocked (loopback)"

    try:
        ip = ipaddress.ip_address(host)
        for net in _BLOCKED_NETS:
            if ip in net:
                return False, f"IP {ip} is in a private/reserved range"
    except ValueError:
        pass  # hostname — DNS resolution happens at crawl time, not here

    return True, ""


# ---------------------------------------------------------------------------
# XSS-safe snippet builder
# ---------------------------------------------------------------------------

def safe_snippet(content: str, terms: list[str], max_len: int = 300) -> str:
    """Return an HTML-safe snippet with search terms wrapped in <mark>."""
    truncated = content[:max_len]
    # Escape ALL HTML first so stored content can never inject tags
    escaped = html.escape(truncated)
    # Then highlight search terms inside the already-escaped string
    for term in terms:
        if term:
            pattern = re.escape(html.escape(term))
            escaped = re.sub(
                f"({pattern})",
                r"<mark>\1</mark>",
                escaped,
                flags=re.IGNORECASE,
            )
    if len(content) > max_len:
        escaped += "…"
    return escaped
