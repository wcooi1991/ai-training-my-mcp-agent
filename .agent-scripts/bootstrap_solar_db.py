# Purpose: verify solar_roi.sqlite bootstrap from CSV folders
# Triggered by: agent after solar MCP server refactor

import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

from mcp_db_server import DB_FILE, ensure_database

test_db = os.path.join(BASE, "solar_roi_test.sqlite")
if os.path.exists(test_db):
    os.remove(test_db)

import mcp_db_server as srv

srv.DB_FILE = test_db
ensure_database()

conn = sqlite3.connect(test_db)
cur = conn.cursor()
for label, sql in [
    ("tnb_rows", "SELECT COUNT(*) FROM tnb_usage"),
    ("solar_oct", "SELECT COUNT(*) FROM solar_readings WHERE data_period='2026-10'"),
    ("assumptions", "SELECT COUNT(*) FROM roi_assumptions"),
    ("views_ok", "SELECT COUNT(*) FROM v_solar_daily_totals"),
]:
    cur.execute(sql)
    print(label, cur.fetchone()[0])
conn.close()
print("ok", test_db)
