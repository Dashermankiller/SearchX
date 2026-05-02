#!/usr/bin/env bash
# ── SearchX production launcher ────────────────────────────────────────────
# Usage:
#   ./run.sh              — start on port 5000 (default)
#   ./run.sh 8080         — start on a custom port
#   ./run.sh --setup-nginx — also write the Nginx config and reload Nginx
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-5000}"
CORES="$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)"
WORKERS=$(( CORES * 2 + 1 ))

echo "╔══════════════════════════════════════════╗"
echo "║            SearchX  — starting           ║"
echo "╚══════════════════════════════════════════╝"
echo "  Directory : $SCRIPT_DIR"
echo "  CPU cores : $CORES"
echo "  Workers   : $WORKERS  (gthread, 4 threads each)"
echo "  Port      : $PORT"
echo ""

cd "$SCRIPT_DIR"

# ── Install / upgrade dependencies ─────────────────────────────────────────
echo "==> Checking dependencies..."
pip install -r requirements.txt -q --upgrade

# ── Optional: write Nginx config ───────────────────────────────────────────
if [[ "${1:-}" == "--setup-nginx" ]]; then
    NGINX_CONF="/etc/nginx/sites-available/searchx"
    echo "==> Writing Nginx config to $NGINX_CONF ..."
    sed "s|SEARCHX_DIR|$SCRIPT_DIR|g" nginx.conf > /tmp/searchx_nginx.conf
    sudo cp /tmp/searchx_nginx.conf "$NGINX_CONF"
    sudo ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/searchx 2>/dev/null || true
    sudo nginx -t && sudo systemctl reload nginx
    echo "==> Nginx configured and reloaded."
    echo ""
fi

# ── Start Gunicorn ─────────────────────────────────────────────────────────
echo "==> Starting Gunicorn..."
echo "    http://127.0.0.1:${PORT}  (or http://0.0.0.0:${PORT} via Nginx)"
echo ""
exec gunicorn wsgi:app \
    --config gunicorn.conf.py \
    --bind "127.0.0.1:${PORT}" \
    --workers "$WORKERS"
