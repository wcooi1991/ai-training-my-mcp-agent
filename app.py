
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile

import altair as alt
import pandas as pd
import streamlit as st
from google.adk.agents import Agent
from google.adk.models import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StdioServerParameters
from google.genai import types

from guardrails import evaluate_input_guardrails as _guardrail_message_for_text

# ==============================================================================
# 1. Page Configuration & Safe Secrets Resolution
# ==============================================================================
st.set_page_config(
    page_title="Solar ROI Virtual Analyst (MCP)",
    page_icon="☀️",
    layout="wide",
    initial_sidebar_state="expanded",
)

api_key = ""
try:
    if "GOOGLE_API_KEY" in st.secrets:
        api_key = st.secrets["GOOGLE_API_KEY"]
except Exception:
    api_key = os.environ.get("GOOGLE_API_KEY", "")

if api_key:
    os.environ["GOOGLE_API_KEY"] = api_key
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")

if not os.environ.get("GOOGLE_API_KEY"):
    st.error(
        "⚠️ **GOOGLE_API_KEY is missing!**\n\n"
        "Go to Streamlit Cloud Dashboard: **App Settings** -> **Secrets** and add:\n"
        "```toml\nGOOGLE_API_KEY = \"your_gemini_api_key_here\"\n```"
    )
    st.stop()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER_SCRIPT = os.path.join(BASE_DIR, "mcp_db_server.py")
DB_FILE = os.path.join(BASE_DIR, "solar_roi.sqlite")
ASSUMPTIONS_MD = os.path.join(BASE_DIR, "roi-calculation-assumptions.md")

REQUIRED_IMPORTS = (
    ("mcp", "mcp"),
    ("sqlparse", "sqlparse"),
    ("google.adk", "google-adk"),
)


def verify_python_environment() -> list[str]:
    """Return human-readable problems if this interpreter cannot run the MCP child process."""
    problems: list[str] = []
    for module, pip_name in REQUIRED_IMPORTS:
        try:
            __import__(module)
        except ImportError:
            problems.append(
                f"Package `{pip_name}` is missing for interpreter:\n`{sys.executable}`"
            )
    if not os.path.isfile(MCP_SERVER_SCRIPT):
        problems.append(f"MCP server script not found: `{MCP_SERVER_SCRIPT}`")
    for folder, label in (
        ("tnb-data", "TNB CSV history"),
        ("solar-data", "October solar CSVs"),
    ):
        path = os.path.join(BASE_DIR, folder)
        if not os.path.isdir(path):
            problems.append(f"Missing data folder `{folder}/` ({label}).")
    return problems


def render_environment_errors(problems: list[str]) -> None:
    st.error(
        "**MCP database tools cannot start** — the child process "
        f"`{sys.executable} mcp_db_server.py` would exit on import.\n\n"
        "The chat message *“MCP SQL is disabled/unavailable”* is **not** from this app; "
        "the model is guessing after a **failed tool call**."
    )
    for p in problems:
        st.markdown(f"- {p}")
    st.code(f"{sys.executable} -m pip install -r requirements.txt", language="bash")
    st.caption("Use the **same** Python that runs `streamlit run app.py` (activate your venv first).")


_ENV_PROBLEMS = verify_python_environment()
if _ENV_PROBLEMS:
    render_environment_errors(_ENV_PROBLEMS)
    st.stop()

# ==============================================================================
# 2. Live Data Evolution Demo (November solar ingest while app runs)
# ==============================================================================
def ensure_solar_db_ready() -> None:
    """Pre-build solar_roi.sqlite in the Streamlit process (same pattern as instant bank seed)."""
    import mcp_db_server as srv

    srv.DB_FILE = DB_FILE
    if os.path.exists(DB_FILE):
        conn = sqlite3.connect(DB_FILE)
        try:
            row = conn.execute(
                "SELECT 1 FROM import_metadata WHERE key = 'bootstrap_complete'"
            ).fetchone()
            if row:
                return
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
    srv.ensure_database()


def load_november_solar_into_db() -> tuple[bool, str]:
    """Ingest new-solar-data/ into SQLite (data_period 2026-11) for runtime discovery."""
    if not os.path.exists(DB_FILE):
        return False, "Database not initialized yet — ask a question first to bootstrap."
    from mcp_db_server import import_november_solar

    conn = sqlite3.connect(DB_FILE)
    try:
        count = import_november_solar(conn)
        conn.commit()
    finally:
        conn.close()
    if count == 0:
        return True, "November 2026 solar data was already loaded (no new rows)."
    return True, f"Loaded {count} interval rows for data_period **2026-11**."


def get_live_ddl() -> str:
    if not os.path.exists(DB_FILE):
        return "Database will be initialized on first MCP server connection."
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    )
    ddl = "\n\n".join([r[0] for r in cur.fetchall() if r[0]])
    conn.close()
    return ddl


def get_assumptions_blurb() -> str:
    if os.path.isfile(ASSUMPTIONS_MD):
        with open(ASSUMPTIONS_MD, encoding="utf-8") as fh:
            return fh.read()
    return "_roi-calculation-assumptions.md not found._"


# ==============================================================================
# 3. Guardrails & privacy
# ==============================================================================
def anonymize_text(text: str) -> str:
    text = re.sub(r"\bOoi Wei Chuen\b", "[homeowner]", text, flags=re.IGNORECASE)
    return text


def before_model_guardrail(callback_context, llm_request):
    last_text = ""
    if llm_request.contents and llm_request.contents[-1].parts:
        last_text = llm_request.contents[-1].parts[0].text or ""
    blocked_msg = _guardrail_message_for_text(last_text)
    if blocked_msg:
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=blocked_msg)],
            )
        )
    return None


def _zero_token_usage() -> dict:
    return {"prompt": 0, "output": 0, "total": 0, "llm_calls": 0}


def _coerce_usage_metadata(um) -> dict | None:
    if um is None:
        return None
    if isinstance(um, dict):
        prompt = um.get("prompt_token_count") or um.get("promptTokenCount") or 0
        output = um.get("candidates_token_count") or um.get("candidatesTokenCount") or 0
        total = um.get("total_token_count") or um.get("totalTokenCount") or 0
    else:
        prompt = getattr(um, "prompt_token_count", 0) or 0
        output = getattr(um, "candidates_token_count", 0) or 0
        total = getattr(um, "total_token_count", 0) or 0
    prompt, output, total = int(prompt), int(output), int(total)
    if total == 0 and (prompt or output):
        total = prompt + output
    if not (prompt or output or total):
        return None
    return {"prompt": prompt, "output": output, "total": total}


def _usage_from_event(event) -> dict | None:
    """Best-effort token counts from ADK / Gemini runner events."""
    metas = []
    direct = getattr(event, "usage_metadata", None)
    if direct is not None:
        metas.append(direct)
    content = getattr(event, "content", None)
    if content is not None:
        metas.append(getattr(content, "usage_metadata", None))
    if hasattr(event, "model_dump"):
        try:
            dumped = event.model_dump(exclude_none=True)
            if dumped.get("usage_metadata"):
                metas.append(dumped["usage_metadata"])
            inner = dumped.get("content") or {}
            if isinstance(inner, dict) and inner.get("usage_metadata"):
                metas.append(inner["usage_metadata"])
        except Exception:
            pass
    for um in metas:
        parsed = _coerce_usage_metadata(um)
        if parsed:
            return parsed
    return None


def _merge_token_usage(base: dict, delta: dict) -> dict:
    return {
        "prompt": base.get("prompt", 0) + delta.get("prompt", 0),
        "output": base.get("output", 0) + delta.get("output", 0),
        "total": base.get("total", 0) + delta.get("total", 0),
        "llm_calls": base.get("llm_calls", 0) + delta.get("llm_calls", 0),
    }


def _gemini_cost_rates_usd_per_1m() -> tuple[float, float]:
    """Illustrative Gemini list rates (USD per 1M tokens); override in Streamlit secrets."""
    try:
        inp = float(st.secrets.get("GEMINI_INPUT_USD_PER_1M", 0.10))
        out = float(st.secrets.get("GEMINI_OUTPUT_USD_PER_1M", 0.40))
    except Exception:
        inp, out = 0.10, 0.40
    return inp, out


def _estimate_cost_usd(prompt_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = _gemini_cost_rates_usd_per_1m()
    return (prompt_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


def render_sidebar_token_usage() -> None:
    st.caption("**Token & cost telemetry**")
    session = st.session_state.get("token_usage") or _zero_token_usage()
    last = st.session_state.get("last_turn_tokens")
    session_cost = _estimate_cost_usd(session.get("prompt", 0), session.get("output", 0))
    c1, c2 = st.columns(2)
    c1.metric("Session tokens", f"{session.get('total', 0):,}")
    c2.metric("Est. session cost", f"${session_cost:.4f}")
    c3, c4 = st.columns(2)
    c3.metric("In / Out", f"{session.get('prompt', 0):,} / {session.get('output', 0):,}")
    c4.metric("LLM calls", f"{session.get('llm_calls', 0):,}")
    if last and last.get("total"):
        last_cost = _estimate_cost_usd(last.get("prompt", 0), last.get("output", 0))
        st.caption(
            f"Last turn: **{last['total']:,}** tokens "
            f"({last.get('prompt', 0):,} in / {last.get('output', 0):,} out) · "
            f"≈ **${last_cost:.4f}**"
        )
    else:
        st.caption("Last turn: —")
    st.markdown("---")


def _function_call_args(raw) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if hasattr(raw, "items"):
        return dict(raw)
    return {}


def _decode_tool_response(raw) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(raw, dict):
        if raw.get("status"):
            return raw
        inner = raw.get("result") or raw.get("output")
        if isinstance(inner, str):
            return _decode_tool_response(inner)
        if isinstance(inner, dict):
            return inner
    return None


def _payload_from_function_response(part) -> tuple[str, dict] | None:
    fr = getattr(part, "function_response", None)
    if not fr:
        return None
    name = getattr(fr, "name", "") or ""
    payload = _decode_tool_response(getattr(fr, "response", None))
    if not payload or payload.get("status") != "success":
        return None
    return name, payload


def render_tnb_stacked_bar_chart(payload: dict) -> None:
    chart_spec = payload.get("stacked_bar_chart")
    if not chart_spec:
        return
    rows = chart_spec.get("rows") or []
    order = chart_spec.get("series_order") or []
    labels = chart_spec.get("series_labels") or {}
    if not rows or not order:
        return

    df = pd.DataFrame(rows)
    long_df = df.melt(id_vars=["day"], value_vars=order, var_name="period_key", value_name="kWh")
    long_df["period"] = long_df["period_key"].map(labels).fillna(long_df["period_key"])
    label_order = [labels.get(k, k) for k in order]
    x_sort = chart_spec.get("x_sort") or sorted(df["day"].unique().tolist())
    x_title = chart_spec.get("x_title", "Day")
    y_title = "Avg usage (kWh)" if chart_spec.get("aggregation") == "weekday_average" else "Usage (kWh)"

    chart = (
        alt.Chart(long_df)
        .mark_bar()
        .encode(
            x=alt.X("day:N", title=x_title, sort=x_sort),
            y=alt.Y("kWh:Q", title=y_title, stack="zero"),
            color=alt.Color(
                "period:N",
                title="Solar window",
                sort=label_order,
                scale=alt.Scale(scheme="tableau10"),
            ),
            tooltip=["day", "period", alt.Tooltip("kWh:Q", format=".2f")],
        )
        .properties(height=360, title=chart_spec.get("title", "TNB usage by solar daypart"))
    )
    st.altair_chart(chart, use_container_width=True)

    avg = payload.get("average_daily_kwh_by_period") or {}
    if avg:
        cols = st.columns(4)
        for col, key in zip(cols, order):
            col.metric(labels.get(key, key), f"{avg.get(key, 0):.2f} kWh")


def render_solar_summary_charts(payload: dict) -> None:
    grouped = payload.get("grouped_bar_chart")
    if grouped and grouped.get("rows"):
        df = pd.DataFrame(grouped["rows"])
        order = grouped.get("series_order") or []
        labels = grouped.get("series_labels") or {}
        long_df = df.melt(id_vars=["day"], value_vars=order, var_name="series_key", value_name="kWh")
        long_df["series"] = long_df["series_key"].map(labels).fillna(long_df["series_key"])
        label_order = [labels.get(k, k) for k in order]
        x_sort = grouped.get("x_sort") or sorted(df["day"].unique().tolist())
        x_title = grouped.get("x_title", "Day")
        y_title = (
            "Avg kWh"
            if grouped.get("aggregation") == "weekday_average"
            else "kWh"
        )
        bar = (
            alt.Chart(long_df)
            .mark_bar()
            .encode(
                x=alt.X("day:N", title=x_title, sort=x_sort),
                y=alt.Y("kWh:Q", title=y_title),
                color=alt.Color("series:N", sort=label_order, title="Flow"),
                xOffset=alt.XOffset("series:N", sort=label_order),
                tooltip=["day", "series", alt.Tooltip("kWh:Q", format=".2f")],
            )
            .properties(height=340, title=grouped.get("title", "Daily solar flows"))
        )
        st.altair_chart(bar, use_container_width=True)

    line_spec = payload.get("line_production_trend")
    if line_spec and line_spec.get("rows"):
        line_df = pd.DataFrame(line_spec["rows"])
        weekday_avg = line_spec.get("aggregation") == "weekday_average"
        x_sort = line_spec.get("x_sort")
        x_title = line_spec.get("x_title", "Day")
        y_title = "Avg production (kWh)" if weekday_avg else "Production (kWh)"
        use_weekday_axis = weekday_avg or (x_sort and line_spec.get("aggregation") != "calendar_day")
        if use_weekday_axis:
            line = (
                alt.Chart(line_df)
                .mark_line(point=True)
                .encode(
                    x=alt.X(
                        "day:N",
                        title=x_title,
                        sort=x_sort or sorted(line_df["day"].unique().tolist()),
                    ),
                    y=alt.Y("production_kwh:Q", title=y_title),
                    tooltip=["day", alt.Tooltip("production_kwh:Q", format=".2f")],
                )
                .properties(height=280, title=line_spec.get("title", "Production trend"))
            )
        else:
            line = (
                alt.Chart(line_df)
                .mark_line(point=True)
                .encode(
                    x=alt.X("day:T", title="Day"),
                    y=alt.Y("production_kwh:Q", title=y_title),
                    tooltip=["day", alt.Tooltip("production_kwh:Q", format=".2f")],
                )
                .properties(height=280, title=line_spec.get("title", "Production trend"))
            )
        st.altair_chart(line, use_container_width=True)

    totals = payload.get("totals") or {}
    if totals:
        cols = st.columns(4)
        cols[0].metric("Production", f"{totals.get('production_kwh', 0):.1f} kWh")
        cols[1].metric("Import", f"{totals.get('import_kwh', 0):.1f} kWh")
        cols[2].metric("Export", f"{totals.get('export_kwh', 0):.1f} kWh")
        cols[3].metric("Self-consumed", f"{totals.get('self_consumed_kwh', 0):.1f} kWh")

    render_solar_financial_overview(payload)


def render_solar_financial_overview(payload: dict) -> None:
    overview = payload.get("financial_overview")
    if not overview:
        return

    amounts = overview.get("amounts_rm") or {}
    st.subheader(overview.get("title", "Estimated Savings & Financial Overview"))
    m1, m2, m3, m4 = st.columns(4)
    if amounts.get("savings_period_total") is not None:
        m1.metric("Savings (period)", f"RM {amounts['savings_period_total']:.2f}")
    if amounts.get("extrapolated_monthly") is not None:
        m2.metric("≈ Monthly savings", f"RM {amounts['extrapolated_monthly']:.2f}")
    if amounts.get("net_investment") is not None:
        net_inv = float(amounts["net_investment"])
        gross = amounts.get("gross_investment")
        inv_help = (
            f"Gross RM {float(gross):,.0f} before SuRIA rebate"
            if gross is not None
            else "Net cost after SuRIA (roi_assumptions)"
        )
        m3.metric("Net investment (panels)", f"RM {net_inv:,.0f}", help=inv_help)
    if amounts.get("payback_years") is not None:
        m4.metric("Indicative payback", f"{amounts['payback_years']:.1f} years")

    with st.expander("How this was calculated", expanded=True):
        rates = overview.get("rates_rm_per_kwh") or {}
        st.caption(
            f"Rates used: **R_avoided** = RM {rates.get('import_avoided', 0):.4f}/kWh (self-consumed), "
            f"**R_export** = RM {rates.get('export_credit_atap', 0):.4f}/kWh (ATAP export credit). "
            f"Window: **{overview.get('days_in_summary', '—')}** day(s) in **{overview.get('data_period', '')}**."
        )
        for i, step in enumerate(overview.get("calculation_steps") or [], start=1):
            st.markdown(f"{i}. **{step.get('step', 'Step')}**")
            st.code(step.get("formula", ""), language=None)
            if step.get("worked_example"):
                st.markdown(f"→ {step['worked_example']}")
        for note in overview.get("caveats") or []:
            st.caption(f"• {note}")


def render_solar_compare_chart(payload: dict) -> None:
    roi = payload.get("roi_comparison") or {}
    if roi:
        st.subheader("ROI impact of period comparison")
        st.caption(roi.get("impact_summary", ""))
        st.code(roi.get("formula", ""), language=None)
        m1, m2, m3 = st.columns(3)
        pa = payload.get("data_period_a", "A")
        pb = payload.get("data_period_b", "B")
        if roi.get("period_a_monthly_rm") is not None:
            m1.metric(f"{pa} ≈ monthly", f"RM {roi['period_a_monthly_rm']:.2f}")
        if roi.get("period_b_monthly_rm") is not None:
            m2.metric(f"{pb} ≈ monthly", f"RM {roi['period_b_monthly_rm']:.2f}")
        if roi.get("delta_monthly_rm") is not None:
            m3.metric("Δ monthly", f"RM {roi['delta_monthly_rm']:+.2f}")
        if roi.get("period_a_payback_years") is not None and roi.get("period_b_payback_years") is not None:
            p1, p2, p3 = st.columns(3)
            p1.metric(f"{pa} payback", f"{roi['period_a_payback_years']:.1f} yr")
            p2.metric(f"{pb} payback", f"{roi['period_b_payback_years']:.1f} yr")
            if roi.get("delta_payback_years") is not None:
                p3.metric("Δ payback", f"{roi['delta_payback_years']:+.1f} yr")

    grouped = payload.get("grouped_period_average_chart")
    if grouped and grouped.get("rows"):
        gdf = pd.DataFrame(grouped["rows"])
        flow_labels = grouped.get("flow_labels") or {}
        label_order = [flow_labels.get(k, k) for k in grouped.get("flow_order") or []]
        period_order = grouped.get("series_order") or sorted(
            gdf["data_period"].unique().tolist()
        )
        bar = (
            alt.Chart(gdf)
            .mark_bar()
            .encode(
                x=alt.X(
                    "flow:N",
                    title="Energy flow",
                    sort=label_order,
                ),
                y=alt.Y("avg_daily_kwh:Q", title="Avg daily kWh"),
                color=alt.Color("data_period:N", title="Period"),
                xOffset=alt.XOffset("data_period:N", sort=period_order),
                tooltip=["data_period", "flow", alt.Tooltip("avg_daily_kwh:Q", format=".2f")],
            )
            .properties(
                height=320,
                title=grouped.get("title", "Average daily flows by period"),
            )
        )
        st.altair_chart(bar, use_container_width=True)

    spec = payload.get("dual_line_chart")
    if spec and spec.get("rows"):
        line_df = pd.DataFrame(spec["rows"])
        y_field = spec.get("y_field", "kwh")
        flow_field = spec.get("color_field", "flow")
        period_field = spec.get("dash_field", "data_period")
        flow_labels = spec.get("flow_labels") or {}
        flow_order = spec.get("flow_order") or []
        flow_sort = [flow_labels.get(k, k) for k in flow_order] or sorted(
            line_df[flow_field].unique().tolist()
        )
        period_order = spec.get("period_order") or sorted(
            line_df[period_field].unique().tolist()
        )
        dash_patterns = [[1, 0], [6, 3], [4, 2], [2, 2], [8, 4], [1, 2]]
        period_dash = dash_patterns[: max(len(period_order), 1)]
        chart = (
            alt.Chart(line_df)
            .mark_line(point=True)
            .encode(
                x=alt.X("day:T", title=spec.get("x_title", "Calendar day")),
                y=alt.Y(f"{y_field}:Q", title="kWh"),
                color=alt.Color(
                    f"{flow_field}:N",
                    title="Flow",
                    sort=flow_sort,
                    legend=alt.Legend(orient="bottom", titleAnchor="start"),
                ),
                strokeDash=alt.StrokeDash(
                    f"{period_field}:N",
                    title="Period",
                    sort=period_order,
                    scale=alt.Scale(domain=period_order, range=period_dash),
                    legend=alt.Legend(orient="bottom", titleAnchor="start"),
                ),
                tooltip=[
                    "day",
                    period_field,
                    flow_field,
                    alt.Tooltip(f"{y_field}:Q", format=".2f"),
                ],
            )
            .properties(height=380, title=spec.get("title", "Period comparison"))
        )
        st.altair_chart(chart, use_container_width=True)

    a = payload.get("period_a_totals") or {}
    b = payload.get("period_b_totals") or {}
    if a and b:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**{payload.get('data_period_a', 'A')}** (period totals)")
            st.metric("Production", f"{a.get('production_kwh', 0):.1f} kWh")
        with c2:
            st.markdown(f"**{payload.get('data_period_b', 'B')}** (period totals)")
            st.metric("Production", f"{b.get('production_kwh', 0):.1f} kWh")


def render_chart_bundle(bundle: dict | None) -> None:
    if not bundle:
        return
    if bundle.get("tnb"):
        render_tnb_stacked_bar_chart(bundle["tnb"])
    if bundle.get("solar"):
        render_solar_summary_charts(bundle["solar"])
    if bundle.get("compare"):
        render_solar_compare_chart(bundle["compare"])


def _store_chart_payload(charts: dict, tool_name: str, payload: dict) -> None:
    if tool_name == "summarize_tnb_by_solar_period":
        charts["tnb"] = payload
    elif tool_name == "summarize_solar_period":
        charts["solar"] = payload
    elif tool_name == "compare_solar_periods":
        charts["compare"] = payload


# ==============================================================================
# 4. Async True-MCP Turn Executor
# ==============================================================================
async def run_true_mcp_turn(prompt: str, history: list) -> tuple[str, list[str], str, dict, dict]:
    mcp_stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    mcp_toolset = McpToolset(
        connection_params=StdioServerParameters(
            command=sys.executable,
            args=[MCP_SERVER_SCRIPT, DB_FILE],
        ),
        tool_filter=[
            "list_tables",
            "list_datasets",
            "describe_table",
            "summarize_solar_period",
            "summarize_tnb_by_solar_period",
            "compare_solar_periods",
            "read_query",
        ],
        errlog=mcp_stderr,
    )

    try:
        agent = Agent(
            name="solar_roi_analyst",
            model="gemini-3.5-flash",
            description=(
                "Virtual analyst for home solar ROI: TNB historical load shapes, "
                "inverter station power (production, grid import/export), and stated tariff assumptions."
            ),
            instruction=(
                "You are **solar_roi_analyst**, an enterprise assistant for residential solar ROI in Malaysia (TNB / ATAP).\n\n"
                "**Data via MCP (read-only SQL):**\n"
                "- **TNB load by daypart:** use `summarize_tnb_by_solar_period(from_date, to_date)` "
                "(stacked_bar_chart = **weekday average** Mon–Sun; daily detail in JSON). "
                "Bands: partial_solar_am (07–10), peak_solar (10–16), partial_solar_pm (16–19), no_solar (19–07).\n"
                "- **Solar inverter data:** use `summarize_solar_period(data_period)` (charts: **weekday-averaged** "
                "grouped bar + **per-calendar-day** production line; detail in `daily_rollup`). For **Oct vs Nov**, "
                "use `compare_solar_periods(a, b)` (multi-flow line chart, grouped averages, "
                "`roi_comparison`) — not two separate summarizes. When explaining ROI impact, cite "
                "`roi_comparison.impact_summary` concisely (monthly savings & payback deltas). Financial figures must match "
                "`financial_overview` from the tool — do not recompute rates in prose. Do **not** query "
                "`solar_readings` via SQL.\n"
                "- Other TNB SQL only when needed: `read_query` on `v_tnb_usage_with_period` (aggregates, max 10 rows).\n"
                "- `roi_assumptions` — rates and investment; `read_query` returns at most **10 rows** "
                "(tool JSON includes `truncation_notice` when more rows matched).\n\n"
                "**Workflow:** `list_datasets` when unsure what's loaded → `summarize_solar_period` for solar ROI → "
                "`summarize_tnb_by_solar_period` for TNB daypart questions (e.g. last week of Aug 2026 → "
                "`2026-08-25` … `2026-08-31`) → "
                "`read_query` only for other TNB/assumption SQL. If `truncation_notice` is present, "
                "tell the user results were truncated to 10 rows and how to refine. "
                "Note seasonality limits when extrapolating.\n"
                "**No shell:** this deployment has **only MCP tools** — you cannot run bash, PowerShell, "
                "pip, or streamlit. Never ask to execute OS commands; guide setup in prose if needed.\n"
                "Be concise and quantitative (kWh, RM, payback years)."
            ),
            tools=[mcp_toolset],
            before_model_callback=before_model_guardrail,
        )

        session_service = InMemorySessionService()
        session_id = "mcp_turn_session"
        session = await session_service.create_session(
            app_name="solar_roi_mcp", user_id="analyst", session_id=session_id
        )
        for m in history[-6:]:
            role = "user" if m["role"] == "user" else "model"
            session.events.append(
                types.Content(role=role, parts=[types.Part.from_text(text=m["content"])])
            )

        runner = Runner(
            agent=agent,
            app_name="solar_roi_mcp",
            session_service=session_service,
            auto_create_session=True,
        )

        msg = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        reply_parts = []
        tools_called = []
        tool_args: dict[str, dict] = {}
        charts: dict = {"tnb": None, "solar": None, "compare": None}
        turn_usage = _zero_token_usage()

        async for event in runner.run_async(
            user_id="analyst", session_id=session_id, new_message=msg
        ):
            usage_delta = _usage_from_event(event)
            if usage_delta:
                turn_usage = _merge_token_usage(
                    turn_usage, {**usage_delta, "llm_calls": 1}
                )
            if hasattr(event, "content") and event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "function_call", None):
                        fc = part.function_call
                        tools_called.append(fc.name)
                        tool_args[fc.name] = _function_call_args(getattr(fc, "args", None))
                    else:
                        parsed_pair = _payload_from_function_response(part)
                        if parsed_pair:
                            _store_chart_payload(charts, parsed_pair[0], parsed_pair[1])
                    if getattr(part, "text", None):
                        reply_parts.append(part.text)

        import mcp_db_server as srv

        srv.DB_FILE = DB_FILE

        if charts["tnb"] is None and "summarize_tnb_by_solar_period" in tools_called:
            args = tool_args.get("summarize_tnb_by_solar_period", {})
            if args.get("from_date") and args.get("to_date"):
                parsed = json.loads(
                    srv.summarize_tnb_by_solar_period(args["from_date"], args["to_date"])
                )
                if parsed.get("status") == "success":
                    charts["tnb"] = parsed

        if charts["solar"] is None and "summarize_solar_period" in tools_called:
            args = tool_args.get("summarize_solar_period", {})
            if args.get("data_period"):
                parsed = json.loads(
                    srv.summarize_solar_period(
                        args["data_period"],
                        args.get("from_date"),
                        args.get("to_date"),
                    )
                )
                if parsed.get("status") == "success":
                    charts["solar"] = parsed

        if charts["compare"] is None and "compare_solar_periods" in tools_called:
            args = tool_args.get("compare_solar_periods", {})
            if args.get("data_period_a") and args.get("data_period_b"):
                parsed = json.loads(
                    srv.compare_solar_periods(
                        args["data_period_a"],
                        args["data_period_b"],
                        args.get("from_date"),
                        args.get("to_date"),
                    )
                )
                if parsed.get("status") == "success":
                    charts["compare"] = parsed

        final_text = "".join(reply_parts) if reply_parts else "No response generated."
        mcp_stderr.seek(0)
        err_tail = mcp_stderr.read().strip()[-4000:]
        bundle = {k: v for k, v in charts.items() if v is not None}
        return anonymize_text(final_text), tools_called, err_tail, bundle, turn_usage
    finally:
        await mcp_toolset.close()
        mcp_stderr.close()


# ==============================================================================
# 5. Sidebar
# ==============================================================================
if "messages" not in st.session_state:
    st.session_state.messages = []
if "token_usage" not in st.session_state:
    st.session_state.token_usage = _zero_token_usage()
if "last_turn_tokens" not in st.session_state:
    st.session_state.last_turn_tokens = None

with st.sidebar:
    st.title("☀️ Solar ROI MCP")
    render_sidebar_token_usage()
    st.caption("ADK `McpToolset` ↔ inverter + TNB SQLite")
    st.caption(f"Python: `{sys.executable}`")
    st.markdown("---")

    st.subheader("⚡ Live data evolution")
    st.caption("Load **November 2026** station CSVs without restarting the app:")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("📥 Load Nov solar", use_container_width=True):
            ok, msg = load_november_solar_into_db()
            (st.success if ok else st.error)(msg)
            st.rerun()
    with c2:
        if st.button("🔄 Reset DB", use_container_width=True):
            if os.path.exists(DB_FILE):
                os.remove(DB_FILE)
            st.warning("Database reset; will re-import TNB + October solar on next question.")
            st.rerun()

    st.markdown("---")
    st.subheader("💡 Suggested questions")
    prompts = [
        "What datasets and date ranges are currently loaded?",
        "Average daily TNB usage (kWh) by solar_period for the last week of August 2026.",
        "Summarize data_period 2026-10: production, import, export, and estimated savings.",
        "Compare solar production: data_period 2026-10 vs 2026-11 (load November first if needed).",
        "drop table solar_readings",
    ]
    for p in prompts:
        if st.button(p, use_container_width=True):
            st.session_state.pending_prompt = p

    st.markdown("---")
    if st.button("🗑️ Clear chat history", use_container_width=True):
        st.session_state.messages = []
        st.session_state.token_usage = _zero_token_usage()
        st.session_state.last_turn_tokens = None
        st.rerun()

    st.markdown("---")
    with st.expander("📄 SQLite schema", expanded=False):
        st.text(get_live_ddl())
    with st.expander("📋 ROI assumptions (markdown)", expanded=False):
        st.markdown(get_assumptions_blurb())

# ==============================================================================
# 6. Main chat
# ==============================================================================
st.title("☀️ Solar ROI Analyst (real MCP)")
st.caption(
    "TNB historical load • October/November inverter logs • ROI assumptions — discovered at runtime via JSON-RPC"
)

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg.get("tools_used"):
            st.caption(
                "🔌 **MCP calls:** " + " → ".join(f"`{t}`" for t in msg["tools_used"])
            )
        if msg.get("charts"):
            render_chart_bundle(msg["charts"])
        elif msg.get("tnb_chart"):
            render_tnb_stacked_bar_chart(msg["tnb_chart"])
        st.markdown(msg["content"])

prompt_to_send = None
if st.session_state.get("pending_prompt"):
    prompt_to_send = st.session_state.pending_prompt
    st.session_state.pending_prompt = None
else:
    user_input = st.chat_input(
        "Ask about usage patterns, solar yield, import/export, or payback…"
    )
    if user_input:
        prompt_to_send = user_input

if prompt_to_send:
    st.session_state.messages.append({"role": "user", "content": prompt_to_send})
    with st.chat_message("user"):
        st.markdown(prompt_to_send)

    with st.chat_message("assistant"):
        blocked_msg = _guardrail_message_for_text(prompt_to_send)
        if blocked_msg:
            reply_text = blocked_msg
            tools_used: list[str] = []
            mcp_err = ""
            charts: dict = {"tnb": None, "solar": None, "compare": None}
            turn_usage = _zero_token_usage()
            st.markdown(reply_text)
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": reply_text,
                    "tools_used": tools_used,
                    "charts": charts,
                }
            )
        else:
            with st.spinner("Preparing database and MCP server…"):
                ensure_solar_db_ready()
                reply_text, tools_used, mcp_err, charts, turn_usage = asyncio.run(
                    run_true_mcp_turn(prompt_to_send, st.session_state.messages[:-1])
                )
                if turn_usage.get("total") or turn_usage.get("prompt") or turn_usage.get("output"):
                    st.session_state.last_turn_tokens = turn_usage
                    st.session_state.token_usage = _merge_token_usage(
                        st.session_state.token_usage, turn_usage
                    )
                else:
                    st.session_state.last_turn_tokens = None
                if tools_used:
                    st.caption(
                        "🔌 **MCP calls:** " + " → ".join(f"`{t}`" for t in tools_used)
                    )
                if mcp_err:
                    with st.expander("⚠️ MCP server stderr (tool may have failed)", expanded=True):
                        st.code(mcp_err)
                render_chart_bundle(charts if charts else None)
                st.markdown(reply_text)
                st.session_state.messages.append(
                    {
                        "role": "assistant",
                        "content": reply_text,
                        "tools_used": tools_used,
                        "charts": charts,
                    }
                )
                if turn_usage.get("total") or turn_usage.get("prompt") or turn_usage.get("output"):
                    st.rerun()
