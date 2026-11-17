"""Check DB schema properly"""
import os
import sqlite3
import sys

db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "recorder.db")
if not os.path.exists(db_path):
    sys.exit(f"Database not found: {db_path}")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# Get all tables and their schemas
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
for t in tables:
    name = t['name']
    cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
    count = conn.execute(f"SELECT COUNT(*) as n FROM {name}").fetchone()['n']
    print(f"\nTable: {name} ({count} rows)")
    for c in cols:
        print(f"  {c['name']} {c['type']}")
    # Show sample data
    rows = conn.execute(f"SELECT * FROM {name} LIMIT 5").fetchall()
    for r in rows:
        print(f"  DATA: {dict(r)}")

conn.close()
