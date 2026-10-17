@echo off
cd /d C:\trading_agents
call .venv\Scripts\activate
python state_change_runner.py --telegram
PAUSE