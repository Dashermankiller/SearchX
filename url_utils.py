"""
URL canonicalisation — strips tracking params, normalises scheme/host,
removes fragments so the crawler never queues two URLs that point to the
same content under different names.
"""

import re
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

# Params that carry no content signal — strip them before queuing
_TRACKING_PARAMS = frozenset({
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'utm_id', 'utm_reader', 'utm_name', 'utm_cid',
    'fbclid', 'gclid', 'gclsrc', 'dclid', 'gbraid', 'wbraid',
    'msclkid', 'yclid', 'igshid', 'twclid', 'ttclid',
    'ref', 'referrer', 'source', 'mc_cid', 'mc_eid',
    '_ga', '_gl', '_hsenc', '_hsmi',
    'zanpid', 'origin',
})

# Schemes we will crawl
_ALLOWED_SCHEMES = frozenset({'http', 'https'})


def canonical(url: str) -> str | None:
    """Return the canonical form of *url*, or None if it should be skipped."""
    try:
        url = url.strip()
        p = urlparse(url)

        if p.scheme not in _ALLOWED_SCHEMES:
            return None
        if not p.netloc:
            return None

        # Skip common non-content file extensions
        path_lower = p.path.lower()
        if any(path_lower.endswith(ext) for ext in (
            '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico', '.bmp',
            '.mp4', '.mp3', '.avi', '.mov', '.webm', '.wav', '.flac',
            '.zip', '.tar', '.gz', '.rar', '.7z',
            '.exe', '.dmg', '.deb', '.rpm', '.msi',
            '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
            '.css', '.js', '.woff', '.woff2', '.ttf', '.eot',
        )):
            return None

        # Lowercase hostname, strip www. for normalisation
        host = p.netloc.lower()
        host = re.sub(r':80$', '', host)    # strip default http port
        host = re.sub(r':443$', '', host)   # strip default https port

        # Strip tracking query params; sort remainder for consistency
        clean_params = sorted(
            (k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
            if k.lower() not in _TRACKING_PARAMS
        )
        query = urlencode(clean_params)

        # Normalise path — collapse // and remove trailing slash on root
        path = re.sub(r'/+', '/', p.path) or '/'

        result = urlunparse((p.scheme, host, path, '', query, ''))
        return result

    except Exception:
        return None


def is_same_domain(url_a: str, url_b: str) -> bool:
    try:
        return urlparse(url_a).netloc.lower() == urlparse(url_b).netloc.lower()
    except Exception:
        return False
