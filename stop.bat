@echo off
cd /d "%~dp0"
docker compose down
echo Stopped. Your data is kept; run start.bat to start again.
pause
