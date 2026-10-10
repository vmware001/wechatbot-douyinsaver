@echo off
rem Frontend helper: sender-service (UIA message sending, port 9999)
rem Auto-restarts if the process exits. Close this window to stop.
chcp 65001 >nul
title FE-sender-service (:9999)
cd /d C:\GSMPSWechatBot
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

:loop
echo [%date% %time%] Starting sender-service...
C:\GSMPSWechatBot\.venv\Scripts\python.exe -u sender_service.py
echo [%date% %time%] sender-service exited. Restarting in 5s... (close window to stop)
timeout /t 5 >nul
goto loop
