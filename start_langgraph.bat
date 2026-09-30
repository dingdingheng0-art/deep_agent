@echo off
chcp 65001 >nul
title LangGraph Dev Server

cd /d "%~dp0"

echo Starting LangGraph dev server on port 2026...
echo.
echo API: http://127.0.0.1:2026
echo Studio UI: https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2026
echo.

uv run langgraph dev --config langgraph.json --port 2026 --no-browser

pause
