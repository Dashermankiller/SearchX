#!/usr/bin/env python3
"""
One-time script to create the PostgreSQL database and user for SearchX.

Run once before starting the app:
    python setup_db.py

Then export the connection URL before starting the server:
    export DATABASE_URL=postgresql://searchx:searchx@localhost:5432/searchx
"""
import subprocess
import sys

DB_NAME = "searchx"
DB_USER = "searchx"
DB_PASS = "searchx"   # change this for production

SQL_STEPS = [
    f"CREATE USER {DB_USER} WITH PASSWORD '{DB_PASS}'",
    f"CREATE DATABASE {DB_NAME} OWNER {DB_USER}",
    f"GRANT ALL PRIVILEGES ON DATABASE {DB_NAME} TO {DB_USER}",
]

print("Setting up PostgreSQL for SearchX…")

for sql in SQL_STEPS:
    result = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-c", sql],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        if "already exists" in result.stderr:
            print(f"  skip (already exists): {sql}")
        else:
            print(f"  ERROR: {result.stderr.strip()}")
            sys.exit(1)
    else:
        print(f"  ok: {sql}")

URL = f"postgresql://{DB_USER}:{DB_PASS}@localhost:5432/{DB_NAME}"
print(f"\nDatabase ready. Add this to your environment:\n  export DATABASE_URL={URL}")
print("\nOr set it in gunicorn.conf.py:\n  raw_env = [\"DATABASE_URL=" + URL + "\"]")
