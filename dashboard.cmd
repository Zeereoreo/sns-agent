@echo off
REM Open the operations dashboard in a browser. Ctrl+C to stop.
REM ASCII-only on purpose (see run_scheduler.cmd).
cd /d "C:\Users\securus\Desktop\agent"
".venv\Scripts\python.exe" dashboard.py
