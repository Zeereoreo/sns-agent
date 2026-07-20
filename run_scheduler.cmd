@echo off
REM One unattended publish run. Called by Windows Task Scheduler 3x/day.
REM ASCII-only on purpose: Korean comments here are mis-decoded as cp949
REM and cmd then tries to execute fragments of them.
cd /d "C:\Users\securus\Desktop\agent"
".venv\Scripts\python.exe" scheduler.py run >> "data\scheduler.log" 2>&1
