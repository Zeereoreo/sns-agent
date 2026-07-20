@echo off
REM 무인 발행 1회 실행 (Windows 작업 스케줄러가 하루 3회 호출)
cd /d "C:\Users\securus\Desktop\agent"
echo. >> "data\scheduler.log"
echo ===== %DATE% %TIME% ===== >> "data\scheduler.log"
".venv\Scripts\python.exe" scheduler.py run >> "data\scheduler.log" 2>&1
