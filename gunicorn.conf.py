"""
Gunicorn production config for SearchX.
Auto-sizes workers relative to CPU count AND PostgreSQL max_connections so we
never exhaust the connection pool under full load.
"""
import multiprocessing
import os

# ── Database ───────────────────────────────────────────────────────────────
raw_env = [
    "DATABASE_URL=postgresql://searchx:searchx@localhost:5432/searchx",
    "PG_MAX_CONNECTIONS=300",
]

# ── Binding ────────────────────────────────────────────────────────────────
bind = "127.0.0.1:5000"

# ── Workers ────────────────────────────────────────────────────────────────
# Each thread holds one persistent PostgreSQL connection, so:
#   workers × threads  ≤  pg max_connections − headroom
#
# With the default pg max_connections = 100 this caps us at 22 workers.
# Run the tuning commands below to raise it to 300 and unlock full CPU capacity,
# then set PG_MAX_CONNECTIONS=300 here (or as an env var).
worker_class     = "gthread"
threads          = 4
_pg_max          = int(os.environ.get("PG_MAX_CONNECTIONS", 100))
_pg_headroom     = 10   # reserve for psql, monitoring, superuser sessions
_max_from_pg     = max(1, (_pg_max - _pg_headroom) // threads)
workers          = min(multiprocessing.cpu_count() * 2 + 1, _max_from_pg)
worker_connections = 1000

# ── Stability ──────────────────────────────────────────────────────────────
max_requests        = 2000
max_requests_jitter = 400
timeout             = 60
graceful_timeout    = 30
keepalive           = 5

# ── Memory efficiency ──────────────────────────────────────────────────────
preload_app = True

if os.path.isdir("/dev/shm"):
    worker_tmp_dir = "/dev/shm"

# ── Logging ────────────────────────────────────────────────────────────────
loglevel          = "info"
accesslog         = "-"
errorlog          = "-"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── Post-fork hook ─────────────────────────────────────────────────────────
def post_fork(server, worker):
    try:
        from database import close_connection
        close_connection()
    except Exception:
        pass
