@echo off
REM ─────────────────────────────────────────────────────────────
REM Start the WebFetch backend (FastAPI / uvicorn, port 8765).
REM This is the long-running service the MCP server talks to.
REM Keep this window open while you use the webfetch tools.
REM Uses %~dp0 so it works wherever this project folder lives.
REM ─────────────────────────────────────────────────────────────
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" "%~dp0server.py"
