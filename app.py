
import asyncio
import os
import re
import sqlite3
import subprocess
import sys
import streamlit as st
from google.genai import types
from google.adk.agents import Agent
from google.adk.models import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StdioServerParameters

# ==============================================================================
# 1. Page Configuration & Safe Secrets Resolution
# ==============================================================================
st.set_page_config(
    page_title="True MCP Banking Virtual Analyst",
    page_icon="🔌",
    layout="wide",
    initial_sidebar_state="expanded"
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
DB_FILE = os.path.join(BASE_DIR, "bank11.sqlite")

# ==============================================================================
# 2. Live Schema Evolution Demo Helpers (Admin Controls)
# ==============================================================================
def execute_demo_migration():
    """Simulates a DBA migrating new tables into bank11.sqlite while app.py is running."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS credit_cards (
        card_id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL,
        card_type TEXT NOT NULL,
        credit_limit REAL NOT NULL,
        outstanding_balance REAL NOT NULL,
        status TEXT NOT NULL
    );
    INSERT OR IGNORE INTO credit_cards VALUES
        ('CC_501', 'CUST_901', 'Visa Infinite', 50000.00, 12450.00, 'ACTIVE'),
        ('CC_502', 'CUST_902', 'Mastercard Platinum', 25000.00, 3120.50, 'ACTIVE'),
        ('CC_503', 'CUST_904', 'Visa Signature', 35000.00, 8900.00, 'ACTIVE'),
        ('CC_504', 'CUST_907', 'Visa Infinite', 100000.00, 45000.00, 'ACTIVE'),
        ('CC_505', 'CUST_908', 'World Elite Mastercard', 200000.00, 0.00, 'ACTIVE');
    """)
    conn.commit()
    conn.close()

def get_live_ddl() -> str:
    if not os.path.exists(DB_FILE):
        return "Database will be initialized on first MCP server connection."
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
    ddl = "\n\n".join([r[0] for r in cur.fetchall() if r[0]])
    conn.close()
    return ddl

# ==============================================================================
# 3. Guardrails & PDPA Anonymizer
# ==============================================================================
def anonymize_text(text: str) -> str:
    text = re.sub(r'\b(CUST_)[0-9A-Za-z]+([0-9A-Za-z]{2})\b', r'\1***\2', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(BR_)[0-9A-Za-z]+([0-9A-Za-z]{2})\b', r'\1***\2', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(ACC_)[0-9A-Za-z]+([0-9A-Za-z]{2})\b', r'\1***\2', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(CC_)[0-9A-Za-z]+([0-9A-Za-z]{2})\b', r'\1***\2', text, flags=re.IGNORECASE)
    return text

def before_model_guardrail(callback_context, llm_request):
    last_text = ""
    if llm_request.contents and llm_request.contents[-1].parts:
        last_text = (llm_request.contents[-1].parts[0].text or "").lower()
    destructive = ["delete", "drop table", "truncate", "modify data", "remove branch", "wipe"]
    if any(k in last_text for k in destructive):
        return LlmResponse(content=types.Content(
            role="model",
            parts=[types.Part(
                text="🛡️ **[GUARDRAIL INTERVENTION]**: Request blocked by Banking Safety Policy. "
                     "Operations are strictly read-only."
            )]
        ))
    return None

# ==============================================================================
# 4. Async True-MCP Turn Executor (Spawns MCPServer Subprocess per Turn)
# ==============================================================================
async def run_true_mcp_turn(prompt: str, history: list) -> tuple[str, list[str]]:
    """Connects to mcp_db_server.py over STDIO JSON-RPC, runs the ADK agent, and closes cleanly."""
    mcp_toolset = McpToolset(
        connection_params=StdioServerParameters(
            command=sys.executable,
            args=[MCP_SERVER_SCRIPT, DB_FILE]
        ),
        tool_filter=["list_tables", "describe_table", "read_query"],
        errlog=subprocess.DEVNULL
    )

    try:
        agent = Agent(
            name="true_mcp_bank_analyst",
            model="gemini-3.5-flash",
            description="Virtual Banking Analyst connected via real MCP JSON-RPC protocol.",
            instruction=(
                "You are an enterprise Virtual Banking Data Analyst connected to an external MCP database server.\n"
                "Always discover available tables via 'list_tables', inspect columns via 'describe_table', "
                "and query data via 'read_query'. Provide concise, executive-ready insights."
            ),
            tools=[mcp_toolset],
            before_model_callback=before_model_guardrail
        )

        session_service = InMemorySessionService()
        session_id = "mcp_turn_session"
        session = await session_service.create_session(
            app_name="true_mcp_cloud", user_id="officer", session_id=session_id
        )

        for m in history[-6:]:
            role = "user" if m["role"] == "user" else "model"
            session.events.append(
                types.Content(role=role, parts=[types.Part.from_text(text=m["content"])])
            )

        runner = Runner(
            agent=agent,
            app_name="true_mcp_cloud",
            session_service=session_service,
            auto_create_session=True
        )

        msg = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        reply_parts = []
        tools_called = []

        async for event in runner.run_async(user_id="officer", session_id=session_id, new_message=msg):
            if hasattr(event, "content") and event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "function_call", None):
                        tools_called.append(part.function_call.name)
                    elif getattr(part, "text", None):
                        reply_parts.append(part.text)

        final_text = "".join(reply_parts) if reply_parts else "No response generated."
        return anonymize_text(final_text), tools_called
    finally:
        await mcp_toolset.close()

# ==============================================================================
# 5. Sidebar Controls & Live Schema Viewer
# ==============================================================================
if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.title("🔌 True MCP Portal")
    st.caption("ADK `McpToolset` ↔ `MCPServer` STDIO")
    st.markdown("---")

    st.subheader("⚡ Live Database Evolution")
    st.caption("Migrate a new table into SQLite to prove zero-code MCP runtime discovery:")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🚀 Add Table", use_container_width=True):
            execute_demo_migration()
            st.success("Added `credit_cards` table!")
            st.rerun()
    with c2:
        if st.button("🔄 Reset DB", use_container_width=True):
            if os.path.exists(DB_FILE):
                os.remove(DB_FILE)
            st.warning("Database reset.")
            st.rerun()

    st.markdown("---")
    st.subheader("💡 Suggested Questions")
    prompts = [
        "What tables currently exist in the MCP server?",
        "List all branches and the number of customers at each.",
        "What is the total balance across all accounts?",
        "What is the total credit limit across active credit cards?",
        "delete the branches table"
    ]
    for p in prompts:
        if st.button(p, use_container_width=True):
            st.session_state.pending_prompt = p

    st.markdown("---")
    if st.button("🗑️ Clear Chat History", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

    st.markdown("---")
    with st.expander("📄 View Live SQLite Schema", expanded=False):
        st.text(get_live_ddl())

# ==============================================================================
# 6. Main Free-Text Chatroom Interface
# ==============================================================================
st.title("🏦 Virtual Banking Analyst (Real MCP Protocol)")
st.caption("Zero SQLite code in app.py • Runtime JSON-RPC Discovery • Protocol-Enforced Least Privilege")

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg.get("tools_used"):
            st.caption("🔌 **MCP JSON-RPC Calls:** " + " → ".join(f"`{t}`" for t in msg["tools_used"]))
        st.markdown(msg["content"])

prompt_to_send = None
if "pending_prompt" in st.session_state and st.session_state.pending_prompt:
    prompt_to_send = st.session_state.pending_prompt
    st.session_state.pending_prompt = None
else:
    user_input = st.chat_input("Ask any question about branches, customers, accounts, or credit cards...")
    if user_input:
        prompt_to_send = user_input

if prompt_to_send:
    st.session_state.messages.append({"role": "user", "content": prompt_to_send})
    with st.chat_message("user"):
        st.markdown(prompt_to_send)

    with st.chat_message("assistant"):
        with st.spinner("Connecting to MCPServer over STDIO JSON-RPC..."):
            reply_text, tools_used = asyncio.run(
                run_true_mcp_turn(prompt_to_send, st.session_state.messages[:-1])
            )
            if tools_used:
                st.caption("🔌 **MCP JSON-RPC Calls:** " + " → ".join(f"`{t}`" for t in tools_used))
            st.markdown(reply_text)
            st.session_state.messages.append({
                "role": "assistant",
                "content": reply_text,
                "tools_used": tools_used
            })
