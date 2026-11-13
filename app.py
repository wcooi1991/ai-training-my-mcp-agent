
import asyncio
import os
import re
import sqlite3
import subprocess
import sys
import tempfile

import streamlit as st
from google.adk.agents import Agent
from google.adk.models import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StdioServerParameters
from google.genai import types

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
        last_text = (llm_request.contents[-1].parts[0].text or "").lower()
    destructive = ["delete", "drop table", "truncate", "modify data", "wipe", "update roi"]
    if any(k in last_text for k in destructive):
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text="🛡️ **[GUARDRAIL]**: Blocked — this analyst is **read-only**. "
                        "Use SQL SELECT on MCP views/tables only."
                    )
                ],
            )
        )
    return None


# ==============================================================================
# 4. Async True-MCP Turn Executor
# ==============================================================================
async def run_true_mcp_turn(prompt: str, history: list) -> tuple[str, list[str], str]:
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
                "- `tnb_usage` — pre-solar household import (30-min kWh); use view `v_tnb_usage_with_period` for "
                "bands: partial_solar_am (07–10), peak_solar (10–16), partial_solar_pm (16–19), no_solar (19–07).\n"
                "- **Solar inverter data:** always use `summarize_solar_period(data_period)` (e.g. `2026-10`, `2026-11`); "
                "optional `from_date` / `to_date` (YYYY-MM-DD). Do **not** query table `solar_readings` via SQL.\n"
                "- TNB: `tnb_usage` / view `v_tnb_usage_with_period` via `read_query` (aggregates preferred).\n"
                "- `roi_assumptions` — rates and investment; `read_query` returns at most **10 rows** "
                "(tool JSON includes `truncation_notice` when more rows matched).\n\n"
                "**Workflow:** `list_datasets` when unsure what's loaded → `summarize_solar_period` for solar ROI → "
                "`read_query` only for TNB patterns or assumptions. If `truncation_notice` is present, "
                "tell the user results were truncated to 10 rows and how to refine. "
                "Note seasonality limits when extrapolating.\n"
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

        async for event in runner.run_async(
            user_id="analyst", session_id=session_id, new_message=msg
        ):
            if hasattr(event, "content") and event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "function_call", None):
                        tools_called.append(part.function_call.name)
                    elif getattr(part, "text", None):
                        reply_parts.append(part.text)

        final_text = "".join(reply_parts) if reply_parts else "No response generated."
        mcp_stderr.seek(0)
        err_tail = mcp_stderr.read().strip()[-4000:]
        return anonymize_text(final_text), tools_called, err_tail
    finally:
        await mcp_toolset.close()
        mcp_stderr.close()


# ==============================================================================
# 5. Sidebar
# ==============================================================================
if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.title("☀️ Solar ROI MCP")
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
        "Compare summarize_solar_period for 2026-10 vs 2026-11 (if November is loaded).",
        "drop table solar_readings",
    ]
    for p in prompts:
        if st.button(p, use_container_width=True):
            st.session_state.pending_prompt = p

    st.markdown("---")
    if st.button("🗑️ Clear chat history", use_container_width=True):
        st.session_state.messages = []
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
        with st.spinner("Preparing database and MCP server…"):
            ensure_solar_db_ready()
            reply_text, tools_used, mcp_err = asyncio.run(
                run_true_mcp_turn(prompt_to_send, st.session_state.messages[:-1])
            )
            if tools_used:
                st.caption(
                    "🔌 **MCP calls:** " + " → ".join(f"`{t}`" for t in tools_used)
                )
            if mcp_err:
                with st.expander("⚠️ MCP server stderr (tool may have failed)", expanded=True):
                    st.code(mcp_err)
            st.markdown(reply_text)
            st.session_state.messages.append(
                {"role": "assistant", "content": reply_text, "tools_used": tools_used}
            )
