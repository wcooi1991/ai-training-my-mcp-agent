
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime
from glob import glob

import sqlparse

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

mcp = MCPServer("SolarROISQLiteMCPServer")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE_DIR, "solar_roi.sqlite")
TNB_DIR = os.path.join(BASE_DIR, "tnb-data")
SOLAR_OCT_DIR = os.path.join(BASE_DIR, "solar-data")
SOLAR_NOV_DIR = os.path.join(BASE_DIR, "new-solar-data")

SOLAR_PERIOD_VIEW_SQL = """
CREATE VIEW IF NOT EXISTS v_tnb_usage_with_period AS
SELECT
    id,
    reading_datetime,
    usage_kwh,
    source_file_date,
    CASE
        WHEN CAST(strftime('%H', reading_datetime) AS INTEGER) >= 7
             AND CAST(strftime('%H', reading_datetime) AS INTEGER) < 10 THEN 'partial_solar_am'
        WHEN CAST(strftime('%H', reading_datetime) AS INTEGER) >= 10
             AND CAST(strftime('%H', reading_datetime) AS INTEGER) < 16 THEN 'peak_solar'
        WHEN CAST(strftime('%H', reading_datetime) AS INTEGER) >= 16
             AND CAST(strftime('%H', reading_datetime) AS INTEGER) < 19 THEN 'partial_solar_pm'
        ELSE 'no_solar'
    END AS solar_period
FROM tnb_usage;

CREATE VIEW IF NOT EXISTS v_solar_interval_kwh AS
SELECT
    id,
    plant_name,
    reading_time,
    data_period,
    production_w / 12000.0 AS production_kwh,
    consumption_w / 12000.0 AS consumption_kwh,
    CASE WHEN grid_flow_w > 0 THEN grid_flow_w / 12000.0 ELSE 0 END AS import_kwh,
    CASE WHEN grid_flow_w < 0 THEN (-grid_flow_w) / 12000.0 ELSE 0 END AS export_kwh
FROM solar_readings;

CREATE VIEW IF NOT EXISTS v_solar_daily_totals AS
SELECT
    data_period,
    date(reading_time) AS day,
    ROUND(SUM(production_w) / 12000.0, 3) AS production_kwh,
    ROUND(SUM(CASE WHEN grid_flow_w > 0 THEN grid_flow_w ELSE 0 END) / 12000.0, 3) AS import_kwh,
    ROUND(SUM(CASE WHEN grid_flow_w < 0 THEN -grid_flow_w ELSE 0 END) / 12000.0, 3) AS export_kwh,
    ROUND(
        SUM(production_w) / 12000.0
        - SUM(CASE WHEN grid_flow_w < 0 THEN -grid_flow_w ELSE 0 END) / 12000.0,
        3
    ) AS self_consumed_kwh
FROM solar_readings
GROUP BY data_period, date(reading_time);
"""


def _parse_tnb_datetime(raw: str) -> str:
    """DD-MM-YYYY HH:MM -> ISO local timestamp for SQLite."""
    dt = datetime.strptime(raw.strip(), "%d-%m-%Y %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tnb_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reading_datetime TEXT NOT NULL,
            usage_kwh REAL NOT NULL,
            source_file_date TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tnb_dt ON tnb_usage(reading_datetime);

        CREATE TABLE IF NOT EXISTS solar_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plant_name TEXT NOT NULL,
            reading_time TEXT NOT NULL,
            production_w REAL NOT NULL,
            consumption_w REAL NOT NULL,
            grid_flow_w REAL NOT NULL,
            battery_w REAL NOT NULL,
            data_period TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_solar_dt ON solar_readings(reading_time);
        CREATE INDEX IF NOT EXISTS idx_solar_period ON solar_readings(data_period);

        CREATE TABLE IF NOT EXISTS roi_assumptions (
            assumption_key TEXT PRIMARY KEY,
            assumption_value REAL,
            text_value TEXT,
            unit TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS import_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    conn.executescript(SOLAR_PERIOD_VIEW_SQL)


def _seed_roi_assumptions(conn: sqlite3.Connection) -> None:
    rows = [
        ("export_credit_atap_rm_per_kwh", 0.2703, None, "RM/kWh", "PathGreen quote; confirm on first ATAP bill"),
        ("import_avoided_rm_per_kwh", 0.4443, None, "RM/kWh", "Gen + capacity + network components (assumed)"),
        ("export_credit_low_rm_per_kwh", 0.218, None, "RM/kWh", "Sensitivity: alternate export credit"),
        ("import_avoided_low_rm_per_kwh", 0.40, None, "RM/kWh", "Sensitivity"),
        ("import_avoided_high_rm_per_kwh", 0.50, None, "RM/kWh", "Sensitivity"),
        ("gross_investment_rm", 17500.0, None, "RM", "Package + EV cable"),
        ("suria_rebate_rm", 3000.0, None, "RM", "RM 600/kWac cap"),
        ("net_investment_rm", 14500.0, None, "RM", "After SuRIA rebate"),
        ("october_monthly_savings_rm", 257.0, None, "RM", "Baseline from logged October"),
        ("headline_payback_years", 4.7, None, "years", "Net investment / monthly savings"),
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO roi_assumptions
        (assumption_key, assumption_value, text_value, unit, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )


def import_tnb_csvs(conn: sqlite3.Connection) -> int:
    paths = sorted(glob(os.path.join(TNB_DIR, "*.csv")))
    inserted = 0
    for path in paths:
        file_date = os.path.splitext(os.path.basename(path))[0]
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                date_key = next((k for k in row if "date" in k.lower()), None)
                usage_key = next((k for k in row if "usage" in k.lower()), None)
                if not date_key or not usage_key or not row.get(date_key):
                    continue
                try:
                    usage = float(row[usage_key].strip())
                except (TypeError, ValueError):
                    continue
                dt_iso = _parse_tnb_datetime(row[date_key])
                conn.execute(
                    """
                    INSERT INTO tnb_usage (reading_datetime, usage_kwh, source_file_date)
                    VALUES (?, ?, ?)
                    """,
                    (dt_iso, usage, file_date),
                )
                inserted += 1
    return inserted


def import_solar_folder(conn: sqlite3.Connection, folder: str, data_period: str) -> int:
    paths = sorted(glob(os.path.join(folder, "*.csv")))
    inserted = 0
    for path in paths:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    conn.execute(
                        """
                        INSERT INTO solar_readings (
                            plant_name, reading_time, production_w, consumption_w,
                            grid_flow_w, battery_w, data_period
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["Plant Name"].strip(),
                            row["Time"].strip(),
                            float(row["Production (W)"]),
                            float(row["Consumption (W)"]),
                            float(row["From Grid[+] / To Grid[-] (W)"]),
                            float(row["Discharge[+] / Charge[-] (W)"]),
                            data_period,
                        ),
                    )
                    inserted += 1
                except (KeyError, ValueError):
                    continue
    return inserted


def import_november_solar(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM solar_readings WHERE data_period = '2026-11'"
    )
    if cur.fetchone()[0] > 0:
        return 0
    return import_solar_folder(conn, SOLAR_NOV_DIR, "2026-11")


def ensure_database() -> None:
    """Load CSVs into SQLite once. Called from tools (not before mcp.run — keeps stdio handshake fast)."""
    if os.path.exists(DB_FILE):
        conn = sqlite3.connect(DB_FILE)
        try:
            cur = conn.execute(
                "SELECT value FROM import_metadata WHERE key = 'bootstrap_complete'"
            )
            if cur.fetchone():
                return
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
        os.remove(DB_FILE)

    conn = sqlite3.connect(DB_FILE)
    try:
        _init_schema(conn)
        import_tnb_csvs(conn)
        import_solar_folder(conn, SOLAR_OCT_DIR, "2026-10")
        _seed_roi_assumptions(conn)
        conn.execute(
            "INSERT OR REPLACE INTO import_metadata (key, value) VALUES ('bootstrap_complete', '1')"
        )
        conn.commit()
    finally:
        conn.close()


READ_QUERY_ROW_CAP = 10

SOLAR_RAW_BLOCKLIST = ("solar_readings", "v_solar_interval_kwh")


def is_sql_mutation(sql_query: str) -> bool:
    cleaned = sql_query.strip().strip(";")
    parsed = sqlparse.parse(cleaned)
    if not parsed:
        return False
    forbidden = {"DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE", "PRAGMA", "REPLACE", "CREATE"}
    for stmt in parsed:
        if stmt.get_type() != "SELECT":
            return True
        for token in stmt.flatten():
            if token.value.upper() in forbidden:
                return True
    return False


def blocks_solar_interval_dump(sql_query: str) -> str | None:
    """Reject queries that would pull 5-min inverter rows into the LLM context."""
    normalized = sql_query.lower()
    for name in SOLAR_RAW_BLOCKLIST:
        if name in normalized:
            return (
                f"Query references `{name}` (interval-level data). "
                "Use `summarize_solar_period` with data_period instead."
            )
    return None


def _fetch_roi_rates(conn: sqlite3.Connection) -> dict[str, float]:
    cur = conn.cursor()
    keys = ("export_credit_atap_rm_per_kwh", "import_avoided_rm_per_kwh", "net_investment_rm")
    out: dict[str, float] = {}
    for key in keys:
        cur.execute(
            "SELECT assumption_value FROM roi_assumptions WHERE assumption_key = ?",
            (key,),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            out[key] = float(row[0])
    return out


@mcp.tool()
def list_tables() -> str:
    """Discover all tables and views in the solar ROI database."""
    ensure_database()
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT name, type FROM sqlite_master
        WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    )
    objects = [{"name": r[0], "kind": r[1]} for r in cur.fetchall()]
    conn.close()
    return json.dumps({"status": "success", "objects": objects})


@mcp.tool()
def list_datasets() -> str:
    """List available TNB history range and solar data periods (e.g. 2026-10, 2026-11)."""
    ensure_database()
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT MIN(reading_datetime), MAX(reading_datetime), COUNT(DISTINCT source_file_date) FROM tnb_usage"
    )
    tnb = cur.fetchone()
    cur.execute(
        """
        SELECT data_period, MIN(date(reading_time)), MAX(date(reading_time)), COUNT(*)
        FROM solar_readings GROUP BY data_period ORDER BY data_period
        """
    )
    solar = [
        {
            "data_period": r[0],
            "from_day": r[1],
            "to_day": r[2],
            "interval_rows": r[3],
        }
        for r in cur.fetchall()
    ]
    conn.close()
    return json.dumps(
        {
            "status": "success",
            "tnb_historical_usage": {
                "from": tnb[0],
                "to": tnb[1],
                "days_loaded": tnb[2],
            },
            "solar_station_power": solar,
            "solar_period_labels": {
                "partial_solar_am": "07:00–10:00",
                "peak_solar": "10:00–16:00",
                "partial_solar_pm": "16:00–19:00",
                "no_solar": "19:00–07:00",
            },
        }
    )


@mcp.tool()
def describe_table(table_name: str) -> str:
    """Inspect columns for a table or view (tnb_usage, solar_readings, roi_assumptions, views)."""
    ensure_database()
    cleaned = table_name.strip().strip("'").strip('"')
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT type FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
        (cleaned,),
    )
    meta = cur.fetchone()
    if not meta:
        conn.close()
        return json.dumps({"status": "error", "message": f"Object '{cleaned}' not found."})
    if meta[0] == "view":
        cur.execute(f"SELECT * FROM {cleaned} LIMIT 0")
        cols = [{"name": d[0], "type": ""} for d in cur.description]
    else:
        cur.execute(f"PRAGMA table_info({cleaned});")
        cols = [{"cid": r[0], "name": r[1], "type": r[2], "pk": r[5]} for r in cur.fetchall()]
    conn.close()
    return json.dumps({"status": "success", "object": cleaned, "kind": meta[0], "columns": cols})


@mcp.tool()
def summarize_solar_period(
    data_period: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> str:
    """
    Compact solar/inverter summary for a data_period (e.g. 2026-10, 2026-11).
    Returns period totals, daily kWh rollup (one row per day), generation by daypart,
    and estimated RM savings using roi_assumptions rates. Does not return 5-min rows.
    Optional from_date / to_date: ISO dates YYYY-MM-DD inclusive.
    """
    ensure_database()
    period = data_period.strip()
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT COUNT(*) FROM solar_readings
        WHERE data_period = ?
        """,
        (period,),
    )
    if cur.fetchone()[0] == 0:
        conn.close()
        return json.dumps(
            {
                "status": "error",
                "message": f"No solar_readings for data_period '{period}'. "
                "Call list_datasets; load November via app if needed.",
            }
        )

    daily_sql = """
        SELECT day, production_kwh, import_kwh, export_kwh, self_consumed_kwh
        FROM v_solar_daily_totals
        WHERE data_period = ?
    """
    params: list[str] = [period]
    if from_date:
        daily_sql += " AND day >= ?"
        params.append(from_date.strip())
    if to_date:
        daily_sql += " AND day <= ?"
        params.append(to_date.strip())
    daily_sql += " ORDER BY day"

    cur.execute(daily_sql, params)
    daily_rows = cur.fetchall()

    totals = {
        "production_kwh": round(sum(r[1] for r in daily_rows), 2),
        "import_kwh": round(sum(r[2] for r in daily_rows), 2),
        "export_kwh": round(sum(r[3] for r in daily_rows), 2),
        "self_consumed_kwh": round(sum(r[4] for r in daily_rows), 2),
        "days_in_summary": len(daily_rows),
    }

    band_sql = """
        SELECT
            CASE
                WHEN CAST(strftime('%H', reading_time) AS INTEGER) >= 7
                     AND CAST(strftime('%H', reading_time) AS INTEGER) < 10 THEN 'partial_solar_am'
                WHEN CAST(strftime('%H', reading_time) AS INTEGER) >= 10
                     AND CAST(strftime('%H', reading_time) AS INTEGER) < 16 THEN 'peak_solar'
                WHEN CAST(strftime('%H', reading_time) AS INTEGER) >= 16
                     AND CAST(strftime('%H', reading_time) AS INTEGER) < 19 THEN 'partial_solar_pm'
                ELSE 'no_solar'
            END AS band,
            ROUND(SUM(production_w) / 12000.0, 2) AS production_kwh
        FROM solar_readings
        WHERE data_period = ?
    """
    band_params: list[str] = [period]
    if from_date:
        band_sql += " AND date(reading_time) >= ?"
        band_params.append(from_date.strip())
    if to_date:
        band_sql += " AND date(reading_time) <= ?"
        band_params.append(to_date.strip())
    band_sql += " GROUP BY band ORDER BY band"

    cur.execute(band_sql, band_params)
    production_by_daypart = {r[0]: r[1] for r in cur.fetchall()}

    rates = _fetch_roi_rates(conn)
    savings_rm = None
    payback_years = None
    if "export_credit_atap_rm_per_kwh" in rates and "import_avoided_rm_per_kwh" in rates:
        savings_rm = round(
            totals["self_consumed_kwh"] * rates["import_avoided_rm_per_kwh"]
            + totals["export_kwh"] * rates["export_credit_atap_rm_per_kwh"],
            2,
        )
        monthly_rm = savings_rm / max(totals["days_in_summary"], 1) * 30
        if monthly_rm > 0 and "net_investment_rm" in rates:
            payback_years = round(rates["net_investment_rm"] / (monthly_rm * 12), 1)

    conn.close()

    daily = [
        {
            "day": r[0],
            "production_kwh": r[1],
            "import_kwh": r[2],
            "export_kwh": r[3],
            "self_consumed_kwh": r[4],
        }
        for r in daily_rows
    ]

    return json.dumps(
        {
            "status": "success",
            "data_period": period,
            "from_date": from_date,
            "to_date": to_date,
            "totals": totals,
            "production_kwh_by_daypart": production_by_daypart,
            "daily_rollup": daily,
            "estimated_savings_rm": savings_rm,
            "extrapolated_monthly_savings_rm": round(savings_rm / max(totals["days_in_summary"], 1) * 30, 2)
            if savings_rm is not None
            else None,
            "indicative_payback_years_from_period": payback_years,
            "rates_used_rm_per_kwh": {
                "import_avoided": rates.get("import_avoided_rm_per_kwh"),
                "export_credit": rates.get("export_credit_atap_rm_per_kwh"),
            },
        }
    )


@mcp.tool()
def read_query(sql_query: str) -> str:
    """
    Read-only SQL for TNB usage, roi_assumptions, and aggregate views
    (e.g. v_tnb_usage_with_period, v_solar_daily_totals with filters).
    For inverter/solar station metrics by month, use summarize_solar_period—not raw solar_readings.
    """
    ensure_database()
    if is_sql_mutation(sql_query):
        return json.dumps(
            {"status": "blocked", "error": "Mutation queries (DROP, DELETE, UPDATE) are prohibited."}
        )
    solar_block = blocks_solar_interval_dump(sql_query)
    if solar_block:
        return json.dumps({"status": "blocked", "error": solar_block})
    try:
        conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(sql_query.strip())
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        conn.close()
        truncated = len(rows) > READ_QUERY_ROW_CAP
        return json.dumps(
            {
                "status": "success",
                "columns": columns,
                "rows": [list(r) for r in rows[:READ_QUERY_ROW_CAP]],
                "count": len(rows),
                "row_cap": READ_QUERY_ROW_CAP,
                "truncated": truncated,
                "truncation_notice": (
                    f"Result set has {len(rows)} rows; only the first {READ_QUERY_ROW_CAP} are returned. "
                    "Add GROUP BY / aggregates, tighten WHERE, or use summarize_solar_period for solar totals."
                )
                if truncated
                else None,
            }
        )
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def wipe_database_admin() -> str:
    """Admin-only destructive tool. Should be stripped by ADK tool_filter."""
    return "DANGER: wipe_database_admin must not be exposed to the agent."


if __name__ == "__main__":
    # Banking demo called ensure_database() here; that was instant. Solar CSV import is
    # heavy — run it from tool handlers (or Streamlit pre-warm) so ADK can connect first.
    mcp.run(transport="stdio")
