@echo off
rem Stop everything: backend (bot brain + supervisor) and frontend (weflow-core + sender-service).
rem Auto-elevates: the bot processes run as admin.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

chcp 65001 >nul
echo Stopping GSMPS Wechat Bot (backend + frontend)...
powershell -NoProfile -ExecutionPolicy Bypass -File C:\GSMPSWechatBot\stop_all.ps1
echo.
echo Done. Terminal windows left open can be closed manually.
pause
