@echo off
cd /d C:\trading_agents
set PYTHONIOENCODING=utf-8
call .venv\Scripts\activate
python daily_runner.py --open --telegram --auto-trade
pause