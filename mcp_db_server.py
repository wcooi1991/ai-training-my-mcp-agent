
import csv
import json
import os
import sqlite3
import sys
from datetime import date, datetime
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


def _parse_tnb_datetime(raw: str) -> str | None:
    """DD-MM-YYYY HH:MM -> ISO local timestamp for SQLite; None if not a reading row."""
    text = (raw or "").strip()
    if not text or text.lower() == "total":
        return None
    try:
        dt = datetime.strptime(text, "%d-%m-%Y %H:%M")
    except ValueError:
        return None
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
                if not dt_iso:
                    continue
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

TNB_SOLAR_PERIOD_ORDER = (
    "partial_solar_am",
    "peak_solar",
    "partial_solar_pm",
    "no_solar",
)

TNB_SOLAR_PERIOD_LABELS = {
    "partial_solar_am": "Partial solar AM (07–10)",
    "peak_solar": "Peak solar (10–16)",
    "partial_solar_pm": "Partial solar PM (16–19)",
    "no_solar": "No solar (19–07)",
}

WEEKDAY_ORDER = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

SOLAR_DAILY_FLOW_ORDER = (
    "production_kwh",
    "import_kwh",
    "export_kwh",
    "self_consumed_kwh",
)

SOLAR_DAILY_FLOW_LABELS = {
    "production_kwh": "Production",
    "import_kwh": "Grid import",
    "export_kwh": "Grid export",
    "self_consumed_kwh": "Self-consumed",
}

# Guardrail: explicit from/to filters may span at most ~3 calendar months.
MAX_DATE_RANGE_DAYS = 92


def _parse_yyyy_mm_dd(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def _date_range_guard_payload(from_date: str, to_date: str) -> dict | None:
    """Return error payload if invalid or range exceeds MAX_DATE_RANGE_DAYS (inclusive)."""
    try:
        start = _parse_yyyy_mm_dd(from_date)
        end = _parse_yyyy_mm_dd(to_date)
    except ValueError:
        return {
            "status": "error",
            "message": "Dates must be ISO format YYYY-MM-DD.",
        }
    if end < start:
        return {
            "status": "error",
            "message": "to_date must be on or after from_date.",
        }
    span_days = (end - start).days + 1
    if span_days > MAX_DATE_RANGE_DAYS:
        return {
            "status": "blocked",
            "error": (
                f"Date range spans {span_days} day(s); maximum is {MAX_DATE_RANGE_DAYS} days (~3 months). "
                "Use a shorter from_date/to_date window or omit both to use the full data_period."
            ),
            "max_range_days": MAX_DATE_RANGE_DAYS,
            "requested_span_days": span_days,
        }
    return None


def _optional_date_range_guard(
    from_date: str | None, to_date: str | None
) -> dict | None:
    if from_date and not to_date:
        return {
            "status": "error",
            "message": "Provide both from_date and to_date, or omit both.",
        }
    if to_date and not from_date:
        return {
            "status": "error",
            "message": "Provide both from_date and to_date, or omit both.",
        }
    if from_date and to_date:
        return _date_range_guard_payload(from_date, to_date)
    return None


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
    keys = (
        "export_credit_atap_rm_per_kwh",
        "import_avoided_rm_per_kwh",
        "net_investment_rm",
        "gross_investment_rm",
        "suria_rebate_rm",
    )
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
            "tnb_summary_tool": "summarize_tnb_by_solar_period",
            "solar_compare_tool": "compare_solar_periods",
        }
    )


@mcp.tool()
def summarize_tnb_by_solar_period(from_date: str, to_date: str) -> str:
    """
    TNB import (kWh) by solar daypart between from_date and to_date (YYYY-MM-DD, inclusive).
    Returns daily detail plus stacked_bar_chart averaged by day-of-week (Mon–Sun) for the UI.
    Date range is limited to 92 days (~3 months) inclusive.
    """
    ensure_database()
    start = from_date.strip()
    end = to_date.strip()
    blocked = _date_range_guard_payload(start, end)
    if blocked:
        return json.dumps(blocked)
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            date(reading_datetime) AS day,
            solar_period,
            ROUND(SUM(usage_kwh), 3) AS usage_kwh
        FROM v_tnb_usage_with_period
        WHERE date(reading_datetime) >= ? AND date(reading_datetime) <= ?
        GROUP BY day, solar_period
        ORDER BY day, solar_period
        """,
        (start, end),
    )
    raw = cur.fetchall()
    conn.close()

    if not raw:
        return json.dumps(
            {
                "status": "error",
                "message": f"No TNB readings between {start} and {end}.",
            }
        )

    by_day: dict[str, dict[str, float]] = {}
    for day, period, kwh in raw:
        by_day.setdefault(day, {p: 0.0 for p in TNB_SOLAR_PERIOD_ORDER})
        if period in by_day[day]:
            by_day[day][period] = float(kwh)

    daily_rows = []
    for day in sorted(by_day):
        row = {"day": day}
        for p in TNB_SOLAR_PERIOD_ORDER:
            row[p] = by_day[day][p]
        row["total_kwh"] = round(sum(by_day[day][p] for p in TNB_SOLAR_PERIOD_ORDER), 3)
        daily_rows.append(row)

    days_count = len(daily_rows)
    averages = {
        p: round(sum(r[p] for r in daily_rows) / days_count, 3) for p in TNB_SOLAR_PERIOD_ORDER
    }
    averages["total_kwh"] = round(sum(r["total_kwh"] for r in daily_rows) / days_count, 3)

    by_weekday: dict[str, list[dict[str, float]]] = {w: [] for w in WEEKDAY_ORDER}
    for day in sorted(by_day):
        weekday = WEEKDAY_ORDER[datetime.strptime(day, "%Y-%m-%d").weekday()]
        by_weekday[weekday].append(by_day[day])

    weekday_chart_rows = []
    for weekday in WEEKDAY_ORDER:
        samples = by_weekday[weekday]
        row = {"day": weekday, "weekday": weekday, "sample_days": len(samples)}
        if not samples:
            for p in TNB_SOLAR_PERIOD_ORDER:
                row[p] = 0.0
        else:
            n = len(samples)
            for p in TNB_SOLAR_PERIOD_ORDER:
                row[p] = round(sum(s[p] for s in samples) / n, 3)
        row["total_kwh"] = round(sum(row[p] for p in TNB_SOLAR_PERIOD_ORDER), 3)
        weekday_chart_rows.append(row)

    return json.dumps(
        {
            "status": "success",
            "from_date": start,
            "to_date": end,
            "days_count": days_count,
            "daily_kwh_by_period": daily_rows,
            "average_daily_kwh_by_period": averages,
            "average_kwh_by_weekday_and_period": weekday_chart_rows,
            "period_labels": TNB_SOLAR_PERIOD_LABELS,
            "stacked_bar_chart": {
                "title": f"Average TNB usage by weekday & solar daypart ({start} to {end})",
                "x_field": "day",
                "x_title": "Day of week",
                "x_sort": list(WEEKDAY_ORDER),
                "aggregation": "weekday_average",
                "series_order": list(TNB_SOLAR_PERIOD_ORDER),
                "series_labels": TNB_SOLAR_PERIOD_LABELS,
                "rows": weekday_chart_rows,
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


def _fetch_solar_daily_rollup(
    conn: sqlite3.Connection,
    period: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
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

    cur = conn.cursor()
    cur.execute(daily_sql, params)
    return [
        {
            "day": r[0],
            "production_kwh": r[1],
            "import_kwh": r[2],
            "export_kwh": r[3],
            "self_consumed_kwh": r[4],
        }
        for r in cur.fetchall()
    ]


def _build_financial_overview(
    totals: dict,
    rates: dict[str, float],
    savings_rm: float | None,
    monthly_rm: float | None,
    payback_years: float | None,
    days_in_summary: int,
    data_period: str,
) -> dict:
    """Structured savings math for UI (matches roi-calculation-assumptions.md)."""
    production = float(totals.get("production_kwh", 0))
    export_kwh = float(totals.get("export_kwh", 0))
    self_kwh = float(totals.get("self_consumed_kwh", 0))
    r_import = float(rates.get("import_avoided_rm_per_kwh", 0))
    r_export = float(rates.get("export_credit_atap_rm_per_kwh", 0))
    net_inv = rates.get("net_investment_rm")

    savings_self = round(self_kwh * r_import, 2)
    savings_export = round(export_kwh * r_export, 2)

    steps = [
        {
            "step": "Self-consumed energy (kWh)",
            "formula": "self_consumed_kwh = production_kwh − export_kwh",
            "worked_example": (
                f"{self_kwh:.2f} = {production:.2f} − {export_kwh:.2f}"
            ),
        },
        {
            "step": "Savings in period (RM)",
            "formula": "S_period = self_consumed_kwh × R_avoided + export_kwh × R_export",
            "worked_example": (
                f"S_period = ({self_kwh:.2f} × {r_import:.4f}) + ({export_kwh:.2f} × {r_export:.4f}) "
                f"= {savings_self:.2f} + {savings_export:.2f}"
                + (f" = {savings_rm:.2f} RM" if savings_rm is not None else "")
            ),
        },
        {
            "step": "Extrapolated monthly savings (RM)",
            "formula": "S_month ≈ S_period ÷ days_in_summary × 30",
            "worked_example": (
                f"S_month ≈ {savings_rm:.2f} ÷ {days_in_summary} × 30 = {monthly_rm:.2f} RM"
                if savings_rm is not None and monthly_rm is not None and days_in_summary
                else "—"
            ),
        },
    ]
    if net_inv and monthly_rm and monthly_rm > 0 and payback_years is not None:
        steps.append(
            {
                "step": "Indicative payback (years)",
                "formula": "payback ≈ net_investment ÷ (S_month × 12)",
                "worked_example": (
                    f"{payback_years:.1f} ≈ {net_inv:.0f} ÷ ({monthly_rm:.2f} × 12)"
                ),
            }
        )

    return {
        "title": "Estimated Savings & Financial Overview",
        "data_period": data_period,
        "days_in_summary": days_in_summary,
        "rates_rm_per_kwh": {
            "import_avoided": r_import,
            "export_credit_atap": r_export,
        },
        "energy_kwh": {
            "production": production,
            "export": export_kwh,
            "self_consumed": self_kwh,
        },
        "amounts_rm": {
            "savings_self_consumed_component": savings_self,
            "savings_export_component": savings_export,
            "savings_period_total": savings_rm,
            "extrapolated_monthly": monthly_rm,
            "net_investment": net_inv,
            "payback_years": payback_years,
        },
        "calculation_steps": steps,
        "caveats": [
            "Rates from roi_assumptions; confirm against your TNB / ATAP bill.",
            "Monthly extrapolation treats this window as typical; monsoon months may differ.",
            "Excludes panel degradation, tariff changes, fixed charges, and inverter replacement.",
        ],
    }


def _weekday_average_daily_rows(
    daily: list[dict], metrics: tuple[str, ...]
) -> list[dict]:
    """Average daily metrics by day-of-week (Mon–Sun) across calendar days in daily."""
    by_weekday: dict[str, list[dict]] = {w: [] for w in WEEKDAY_ORDER}
    for row in daily:
        weekday = WEEKDAY_ORDER[datetime.strptime(row["day"], "%Y-%m-%d").weekday()]
        by_weekday[weekday].append(row)

    out: list[dict] = []
    for weekday in WEEKDAY_ORDER:
        samples = by_weekday[weekday]
        agg = {"day": weekday, "weekday": weekday, "sample_days": len(samples)}
        if not samples:
            for key in metrics:
                agg[key] = 0.0
        else:
            n = len(samples)
            for key in metrics:
                agg[key] = round(sum(float(s[key]) for s in samples) / n, 3)
        out.append(agg)
    return out


def _solar_period_has_data(conn: sqlite3.Connection, period: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM solar_readings WHERE data_period = ?",
        (period,),
    )
    return cur.fetchone()[0] > 0


@mcp.tool()
def summarize_solar_period(
    data_period: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> str:
    """
    Compact solar/inverter summary for a data_period (e.g. 2026-10, 2026-11).
    Returns period totals, daily kWh rollup, generation by daypart, RM savings, and
    grouped_bar_chart averaged by day-of-week (Mon–Sun); line_production_trend by calendar day.
    Optional from_date / to_date: ISO dates YYYY-MM-DD inclusive (max ~3 months when both set).
    """
    ensure_database()
    period = data_period.strip()
    range_block = _optional_date_range_guard(
        from_date.strip() if from_date else None,
        to_date.strip() if to_date else None,
    )
    if range_block:
        return json.dumps(range_block)
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)

    if not _solar_period_has_data(conn, period):
        conn.close()
        return json.dumps(
            {
                "status": "error",
                "message": f"No solar_readings for data_period '{period}'. "
                "Call list_datasets; load November via app if needed.",
            }
        )

    daily = _fetch_solar_daily_rollup(conn, period, from_date, to_date)
    daily_rows = [
        (d["day"], d["production_kwh"], d["import_kwh"], d["export_kwh"], d["self_consumed_kwh"])
        for d in daily
    ]

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

    cur = conn.cursor()
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
    else:
        monthly_rm = None

    conn.close()

    weekday_rows = _weekday_average_daily_rows(daily, SOLAR_DAILY_FLOW_ORDER)
    financial_overview = None
    if savings_rm is not None:
        financial_overview = _build_financial_overview(
            totals,
            rates,
            savings_rm,
            round(monthly_rm, 2) if monthly_rm is not None else None,
            payback_years,
            totals["days_in_summary"],
            period,
        )

    return json.dumps(
        {
            "status": "success",
            "data_period": period,
            "from_date": from_date,
            "to_date": to_date,
            "totals": totals,
            "production_kwh_by_daypart": production_by_daypart,
            "daily_rollup": daily,
            "average_kwh_by_weekday": weekday_rows,
            "estimated_savings_rm": savings_rm,
            "extrapolated_monthly_savings_rm": round(monthly_rm, 2) if monthly_rm is not None else None,
            "indicative_payback_years_from_period": payback_years,
            "financial_overview": financial_overview,
            "rates_used_rm_per_kwh": {
                "import_avoided": rates.get("import_avoided_rm_per_kwh"),
                "export_credit": rates.get("export_credit_atap_rm_per_kwh"),
            },
            "grouped_bar_chart": {
                "title": f"Avg daily energy flows by weekday (kWh) — {period}",
                "x_field": "day",
                "x_title": "Day of week",
                "x_sort": list(WEEKDAY_ORDER),
                "aggregation": "weekday_average",
                "series_order": list(SOLAR_DAILY_FLOW_ORDER),
                "series_labels": SOLAR_DAILY_FLOW_LABELS,
                "rows": weekday_rows,
            },
            "line_production_trend": {
                "title": f"Daily production (kWh) — {period}",
                "x_field": "day",
                "x_title": "Calendar day",
                "aggregation": "calendar_day",
                "y_field": "production_kwh",
                "rows": [
                    {
                        "day": d["day"],
                        "production_kwh": float(d["production_kwh"]),
                    }
                    for d in daily
                ],
            },
        }
    )


def _financial_metrics_from_daily(
    daily: list[dict], rates: dict[str, float]
) -> dict:
    """Period savings / monthly extrapolation / payback from daily rollup rows."""
    days = len(daily)
    if days == 0:
        return {}
    totals = {key: round(sum(float(r[key]) for r in daily), 2) for key in SOLAR_DAILY_FLOW_ORDER}
    r_import = float(rates.get("import_avoided_rm_per_kwh", 0))
    r_export = float(rates.get("export_credit_atap_rm_per_kwh", 0))
    net_inv = rates.get("net_investment_rm")
    savings_period = round(
        totals["self_consumed_kwh"] * r_import + totals["export_kwh"] * r_export,
        2,
    )
    monthly = round(savings_period / days * 30, 2)
    payback = None
    if net_inv and monthly > 0:
        payback = round(float(net_inv) / (monthly * 12), 1)
    return {
        "days": days,
        "totals_kwh": totals,
        "savings_period_rm": savings_period,
        "extrapolated_monthly_savings_rm": monthly,
        "indicative_payback_years": payback,
    }


def _avg_daily_kwh_by_flow(daily: list[dict]) -> dict[str, float]:
    if not daily:
        return {k: 0.0 for k in SOLAR_DAILY_FLOW_ORDER}
    n = len(daily)
    return {k: round(sum(float(r[k]) for r in daily) / n, 3) for k in SOLAR_DAILY_FLOW_ORDER}


@mcp.tool()
def compare_solar_periods(
    data_period_a: str,
    data_period_b: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> str:
    """
    Compare two data_period values (e.g. 2026-10 vs 2026-11): energy flows, dual line chart,
    grouped average-by-flow bar chart, and roi_comparison (monthly savings & payback impact).
    Optional from_date / to_date (max ~3 months when both set).
    """
    ensure_database()
    period_a = data_period_a.strip()
    period_b = data_period_b.strip()
    range_block = _optional_date_range_guard(
        from_date.strip() if from_date else None,
        to_date.strip() if to_date else None,
    )
    if range_block:
        return json.dumps(range_block)
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)

    missing = [
        p for p in (period_a, period_b) if not _solar_period_has_data(conn, p)
    ]
    if missing:
        conn.close()
        return json.dumps(
            {
                "status": "error",
                "message": f"No solar data for period(s): {', '.join(missing)}. "
                "Use list_datasets; load November from the app if needed.",
            }
        )

    daily_a = _fetch_solar_daily_rollup(conn, period_a, from_date, to_date)
    daily_b = _fetch_solar_daily_rollup(conn, period_b, from_date, to_date)
    rates = _fetch_roi_rates(conn)
    conn.close()

    fin_a = _financial_metrics_from_daily(daily_a, rates)
    fin_b = _financial_metrics_from_daily(daily_b, rates)
    monthly_a = fin_a.get("extrapolated_monthly_savings_rm")
    monthly_b = fin_b.get("extrapolated_monthly_savings_rm")
    payback_a = fin_a.get("indicative_payback_years")
    payback_b = fin_b.get("indicative_payback_years")

    delta_monthly = None
    delta_payback = None
    if monthly_a is not None and monthly_b is not None:
        delta_monthly = round(monthly_b - monthly_a, 2)
    if payback_a is not None and payback_b is not None:
        delta_payback = round(payback_b - payback_a, 1)

    r_import = rates.get("import_avoided_rm_per_kwh", 0)
    r_export = rates.get("export_credit_atap_rm_per_kwh", 0)
    impact_summary = (
        "ROI uses the same formula as a single-period summary: "
        f"S = self_consumed × {r_import:.4f} + export × {r_export:.4f} (RM), "
        "then S_month ≈ S ÷ days × 30 and payback ≈ net_investment ÷ (S_month × 12). "
        "Comparing periods shows how a shift in yield (production / self-consumption / export) "
        "changes extrapolated monthly savings and payback—use the lower month when stress-testing seasonality."
    )
    if delta_monthly is not None:
        direction = "higher" if delta_monthly > 0 else "lower" if delta_monthly < 0 else "unchanged"
        impact_summary += (
            f" Here, {period_b} vs {period_a}: ≈ RM {abs(delta_monthly):.2f}/month {direction}"
            + (
                f"; payback {delta_payback:+.1f} year(s)."
                if delta_payback is not None
                else "."
            )
        )

    line_rows: list[dict] = []
    for row in daily_a:
        for flow_key in SOLAR_DAILY_FLOW_ORDER:
            line_rows.append(
                {
                    "day": row["day"],
                    "data_period": period_a,
                    "flow_key": flow_key,
                    "flow": SOLAR_DAILY_FLOW_LABELS[flow_key],
                    "kwh": float(row[flow_key]),
                }
            )
    for row in daily_b:
        for flow_key in SOLAR_DAILY_FLOW_ORDER:
            line_rows.append(
                {
                    "day": row["day"],
                    "data_period": period_b,
                    "flow_key": flow_key,
                    "flow": SOLAR_DAILY_FLOW_LABELS[flow_key],
                    "kwh": float(row[flow_key]),
                }
            )

    avg_a = _avg_daily_kwh_by_flow(daily_a)
    avg_b = _avg_daily_kwh_by_flow(daily_b)
    grouped_rows: list[dict] = []
    for flow_key in SOLAR_DAILY_FLOW_ORDER:
        grouped_rows.append(
            {
                "data_period": period_a,
                "flow_key": flow_key,
                "flow": SOLAR_DAILY_FLOW_LABELS[flow_key],
                "avg_daily_kwh": avg_a[flow_key],
            }
        )
        grouped_rows.append(
            {
                "data_period": period_b,
                "flow_key": flow_key,
                "flow": SOLAR_DAILY_FLOW_LABELS[flow_key],
                "avg_daily_kwh": avg_b[flow_key],
            }
        )

    return json.dumps(
        {
            "status": "success",
            "data_period_a": period_a,
            "data_period_b": period_b,
            "from_date": from_date,
            "to_date": to_date,
            "period_a_financials": fin_a,
            "period_b_financials": fin_b,
            "period_a_totals": {
                "days": fin_a.get("days", 0),
                **(fin_a.get("totals_kwh") or {k: 0.0 for k in SOLAR_DAILY_FLOW_ORDER}),
            },
            "period_b_totals": {
                "days": fin_b.get("days", 0),
                **(fin_b.get("totals_kwh") or {k: 0.0 for k in SOLAR_DAILY_FLOW_ORDER}),
            },
            "roi_comparison": {
                "impact_summary": impact_summary,
                "formula": "S = self_consumed_kwh × R_avoided + export_kwh × R_export; S_month ≈ S ÷ days × 30",
                "rates_rm_per_kwh": {
                    "import_avoided": r_import,
                    "export_credit_atap": r_export,
                },
                "period_a_monthly_rm": monthly_a,
                "period_b_monthly_rm": monthly_b,
                "delta_monthly_rm": delta_monthly,
                "period_a_payback_years": payback_a,
                "period_b_payback_years": payback_b,
                "delta_payback_years": delta_payback,
            },
            "dual_line_chart": {
                "title": f"Daily energy flows — {period_a} vs {period_b}",
                "x_field": "day",
                "x_title": "Calendar day",
                "aggregation": "calendar_day",
                "y_field": "kwh",
                "series_field": "data_period",
                "stroke_field": "flow",
                "rows": line_rows,
            },
            "grouped_period_average_chart": {
                "title": f"Average daily kWh by flow — {period_a} vs {period_b}",
                "x_field": "flow",
                "series_field": "data_period",
                "y_field": "avg_daily_kwh",
                "series_order": [period_a, period_b],
                "flow_order": list(SOLAR_DAILY_FLOW_ORDER),
                "flow_labels": SOLAR_DAILY_FLOW_LABELS,
                "rows": grouped_rows,
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
