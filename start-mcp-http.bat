@echo off
REM ─────────────────────────────────────────────────────────────
REM Start the WebFetch MCP server over HTTP (streamable-http).
REM Endpoint: http://127.0.0.1:8766/mcp  (path /mcp).
REM
REM This is for remote / Ngrok / Chat custom-connector POCs. The
REM stdio transport (spawned by Claude) is unaffected and can run
REM at the same time. The backend (start-server.bat, port 8765)
REM MUST also be running — these tools proxy to it.
REM
REM For Ngrok exposure, set MCP_HTTP_HOST=0.0.0.0 before running,
REM then: ngrok http 8766  → add the public URL + /mcp as a
REM custom connector in the Chat interface.
REM ─────────────────────────────────────────────────────────────
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" "%~dp0mcp_server.py" --http
