@echo off
rem Start everything in ONE Windows Terminal window with 3 tabs:
rem   tab1 (this one): backend supervisor (bot brain)
rem   tab2: FE-weflow-core (:5031, DB decrypt + SSE push + media)
rem   tab3: FE-sender-service (:9999, UIA message sending)
rem Existing bot processes are killed first. Auto-elevates (WeChat runs as admin).
rem NOTE: keep this file ASCII-only. Chinese chars + chcp 65001 break cmd's
rem batch parser byte offsets and cause chaotic re-execution (window spam).

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    where wt.exe >nul 2>&1
    if %errorlevel%==0 (
        powershell -NoProfile -Command "Start-Process wt.exe -Verb RunAs -ArgumentList '--','cmd','/c','%~f0'"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    )
    exit /b
)

chcp 65001 >nul
cd /d C:\GSMPSWechatBot
title BE-brain

echo [0/3] Killing any running bot processes first...
powershell -NoProfile -ExecutionPolicy Bypass -File C:\GSMPSWechatBot\stop_all.ps1
timeout /t 3 >nul

echo [1/3] Starting frontend services in new tabs...
where wt.exe >nul 2>&1
if %errorlevel%==0 (
    wt -w 0 new-tab --title "FE-weflow:5031" --suppressApplicationTitle cmd /c C:\GSMPSWechatBot\frontend_weflow.bat
    wt -w 0 new-tab --title "FE-sender:9999" --suppressApplicationTitle cmd /c C:\GSMPSWechatBot\frontend_sender.bat
    wt -w 0 new-tab --title "FE-keepalive" --suppressApplicationTitle cmd /c C:\GSMPSWechatBot\_svc_loop.bat FE-keepalive C:\GSMPSWechatBot\.venv\Scripts\python.exe C:\GSMPSWechatBot\tools\fe_keepalive.py
) else (
    start "FE-weflow-core" cmd /c C:\GSMPSWechatBot\frontend_weflow.bat
    start "FE-sender-service" cmd /c C:\GSMPSWechatBot\frontend_sender.bat
    start "FE-keepalive" cmd /c C:\GSMPSWechatBot\_svc_loop.bat FE-keepalive C:\GSMPSWechatBot\.venv\Scripts\python.exe C:\GSMPSWechatBot\tools\fe_keepalive.py
)

echo [2/3] Waiting for frontend health (:5031 and :9999)...
powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 90;$i++){ try { $a=Invoke-RestMethod -Uri 'http://127.0.0.1:5031/health' -TimeoutSec 2; $b=Invoke-RestMethod -Uri 'http://127.0.0.1:9999/health' -TimeoutSec 2; if($a.service -eq 'weflow-core' -and $b.service -eq 'sender'){ $ok=$true; break } } catch {}; Start-Sleep -Seconds 1 }; if($ok){ exit 0 } else { exit 1 }"
if %errorlevel% neq 0 (
    echo [WARN] Frontend health check timed out. Starting backend anyway - it will keep waiting on its own.
) else (
    echo       Frontend is ready.
)

echo [3/3] Starting backend (bot brain, auto-restart supervisor)...
call C:\GSMPSWechatBot\start_bot_vm.bat
