"""Check DB schema properly"""
import sqlite3

db_path = r"c:\Users\User\loyiha\record\recorder.db"
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
