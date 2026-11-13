# ai-training-my-mcp-agent

Solar ROI **real MCP** demo: Streamlit + Google ADK agent `solar_roi_analyst` talks to `mcp_db_server.py` over stdio. Data folders: `tnb-data/` (historical load), `solar-data/` (Oct 2026 inverter), `new-solar-data/` (Nov 2026, loaded via sidebar). Assumptions in `roi-calculation-assumptions.md`, seeded into table `roi_assumptions`.

**In-app guardrails:** `guardrails.py` blocks shell/terminal requests and data-mutation prompts before the ADK agent runs (`before_model_callback` + chat UI). The agent only has read-only MCP tools.

**Cursor (optional):** `.cursor/hooks.json` can deny Shell in the IDE; see `.cursor/rules/destructive-shell-block.mdc`.
