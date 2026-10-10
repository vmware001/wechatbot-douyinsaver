@echo off
rem Frontend helper: weflow-core (DB decrypt + SSE push + media, port 5031)
rem Auto-restarts if the process exits. Close this window to stop.
chcp 65001 >nul
title FE-weflow-core (:5031)
cd /d C:\GSMPSWechatBot\weflow-core

:loop
echo [%date% %time%] Starting weflow-core...
WeFlow.exe dist\index.js C:\GSMPSWechatBot\config.json
echo [%date% %time%] weflow-core exited. Restarting in 5s... (close window to stop)
timeout /t 5 >nul
goto loop
