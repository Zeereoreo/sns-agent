@echo off
REM One unattended publish run. Called by Windows Task Scheduler 3x/day.
REM ASCII-only on purpose: Korean comments here are mis-decoded as cp949
REM and cmd then tries to execute fragments of them.
cd /d "C:\Users\securus\Desktop\agent"
REM Random 0-9 min jitter so the actual post time varies daily (avoid a fixed-time footprint).
".venv\Scripts\python.exe" -c "import random,time; time.sleep(random.randint(0,540))"
".venv\Scripts\python.exe" scheduler.py run >> "data\scheduler.log" 2>&1
