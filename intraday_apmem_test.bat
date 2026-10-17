@echo off
cd /d C:\trading_agents
call .venv\Scripts\activate
py intraday_apmem_test.py
pause