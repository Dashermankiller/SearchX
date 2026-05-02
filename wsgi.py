"""WSGI entry point for Gunicorn / uWSGI."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app import app  # noqa: F401 — Gunicorn looks for 'app' in this module
