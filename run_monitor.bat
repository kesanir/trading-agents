@echo off
cd /d C:\trading_agents
set PYTHONIOENCODING=utf-8
call .venv\Scripts\activate
python -m app.services.trading_runner
PAUSE
