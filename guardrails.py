"""Input policy for the Solar ROI Streamlit app (no shell, read-only data)."""

from __future__ import annotations

import re

_READONLY_DATA_KEYWORDS = (
    "delete",
    "drop table",
    "truncate",
    "modify data",
    "wipe",
    "update roi",
)

_SHELL_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pat, re.I)
    for pat in (
        r"\bsudo\s+",
        r"\brm\s+(-[^\s]*r|-rf|-fr|--recursive|--force)\b",
        r"\bformat\s+[a-zA-Z]:",
        r"\bdiskpart\b",
        r"\bdd\s+if=",
        r"\b(rd|rmdir)\s+/s",
        r"\bdel\s+/[fqs]",
        r"\bRemove-Item\b[^\n]*(-Recurse|-Force)",
        r"\b(powershell|pwsh|cmd)\s+(-c|/c)\s",
        r"\b(bash|sh|zsh)\s+(-c|-lc)\s",
        r"\bcurl\s+[^\s|]+\s*\|\s*(bash|sh)\b",
        r"\bwget\s+[^\s|]+\s*\|\s*(bash|sh)\b",
        r"\bInvoke-Expression\b",
        r"\bos\.system\s*\(",
        r"\bsubprocess\.(run|call|Popen)\b",
    )
)

_SHELL_INTENT_PHRASES = (
    "run in terminal",
    "run this command",
    "execute this command",
    "execute the command",
    "run shell",
    "shell command",
    "terminal command",
    "run pip install",
    "run streamlit",
    "open terminal and",
)


def evaluate_input_guardrails(user_text: str) -> str | None:
    """
    Return a user-facing guardrail message when the prompt is out of policy,
    or None if the message may proceed to the agent.
    """
    if not (user_text or "").strip():
        return None
    lowered = user_text.lower()

    if any(k in lowered for k in _READONLY_DATA_KEYWORDS):
        return (
            "🛡️ **[GUARDRAIL]**: Blocked — this analyst is **read-only**. "
            "Use SQL SELECT on MCP views/tables only (via MCP tools)."
        )

    for pattern in _SHELL_COMMAND_PATTERNS:
        if pattern.search(user_text):
            return (
                "🛡️ **[GUARDRAIL]**: Blocked — this app has **no shell or OS access**. "
                "The solar analyst only calls **read-only MCP tools** (`summarize_*`, "
                "`compare_solar_periods`, `read_query`, etc.). "
                "Run installs or server commands in **your own terminal**, not through chat."
            )

    if any(phrase in lowered for phrase in _SHELL_INTENT_PHRASES):
        return (
            "🛡️ **[GUARDRAIL]**: Blocked — cannot run terminal or shell commands from this UI. "
            "Ask about TNB/solar data and ROI via MCP, or run local commands yourself outside Streamlit."
        )

    return None
