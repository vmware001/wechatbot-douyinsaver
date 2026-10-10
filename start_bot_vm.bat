@echo off
rem GSMPSWechatBot supervisor for this VM: auto-restarts main.py if it crashes.
rem Auto-elevates because WeChat runs as admin (UIPI blocks normal-permission control).

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
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
set PATH=C:\Program Files\nodejs;%PATH%

rem --- one-window mode: not inside Windows Terminal yet and wt exists ->
rem respawn the whole stack as ONE wt window with 3 tabs (brain + weflow +
rem sender); this plain window then exits. The respawned copy sees
rem WT_SESSION and skips straight to fe_setup.
if defined WT_SESSION goto fe_setup
where wt.exe >nul 2>&1
if not errorlevel 1 goto spawn_wt
goto fe_setup

:spawn_wt
rem include frontend tabs only for services that are not already healthy
rem (e.g. legacy windows from before the consolidation may still hold them)
powershell -NoProfile -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:5031/health' -TimeoutSec 2; if ($r.service -eq 'weflow-core') { exit 0 } else { exit 1 } } catch { exit 1 }"
set NEED_WE=%errorlevel%
powershell -NoProfile -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:9999/health' -TimeoutSec 2; if ($r.service -eq 'sender') { exit 0 } else { exit 1 } } catch { exit 1 }"
set NEED_SE=%errorlevel%
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'fe_keepalive\.py' }) { exit 0 } else { exit 1 }"
set NEED_KA=%errorlevel%
set WTCMD=wt -w new --title BE-brain cmd /c "%~f0"
if %NEED_WE% neq 0 set WTCMD=%WTCMD% ; new-tab --title FE-weflow:5031 cmd /c C:\GSMPSWechatBot\frontend_weflow.bat
if %NEED_SE% neq 0 set WTCMD=%WTCMD% ; new-tab --title FE-sender:9999 cmd /c C:\GSMPSWechatBot\frontend_sender.bat
if %NEED_KA% neq 0 set WTCMD=%WTCMD% ; new-tab --title FE-keepalive cmd /c C:\GSMPSWechatBot\_svc_loop.bat FE-keepalive C:\GSMPSWechatBot\.venv\Scripts\python.exe C:\GSMPSWechatBot\tools\fe_keepalive.py
echo Spawning unified Windows Terminal window (brain + missing frontends)...
%WTCMD%
exit /b

:fe_setup
:loop
rem ensure frontend services are up before the brain waits on them:
rem weflow-core (:5031, SSE message source) and sender-service (:9999, UIA send).
rem When we just spawned them as sibling wt tabs they need a moment, so
rem wait up to 90s before concluding one is missing (avoids duplicates).
set FE_WAIT=0
:fe_check
powershell -NoProfile -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:5031/health' -TimeoutSec 2; if ($r.service -eq 'weflow-core') { exit 0 } else { exit 1 } } catch { exit 1 }"
set W5031=%errorlevel%
powershell -NoProfile -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:9999/health' -TimeoutSec 2; if ($r.service -eq 'sender') { exit 0 } else { exit 1 } } catch { exit 1 }"
set W9999=%errorlevel%
if "%W5031%"=="0" if "%W9999%"=="0" goto fe_ready
if %FE_WAIT% GEQ 90 goto fe_start_missing
timeout /t 5 >nul
set /a FE_WAIT+=5
goto fe_check
:fe_start_missing
rem Never spawn a second copy of a frontend loop: each one is an endless
rem restart loop, so duplicates pile up windows and fight over the same port.
rem (Before this guard, every fe_check timeout added another window.)
if %W5031% neq 0 goto fe_start_weflow
if %W9999% neq 0 goto fe_start_sender
goto fe_ready

:fe_start_weflow
set /a WE_FAILS+=1
if %WE_FAILS% GEQ 3 goto fe_weflow_backoff
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'frontend_weflow\.bat' }) { exit 0 } else { exit 1 }"
if not errorlevel 1 (
    echo [%date% %time%] weflow-core :5031 not up - a frontend loop already exists, not starting another.
    goto fe_start_sender_check
)
echo [%date% %time%] weflow-core :5031 not up - starting it...
start "FE-weflow-core" /min cmd /c C:\GSMPSWechatBot\frontend_weflow.bat
goto fe_start_sender_check

:fe_weflow_backoff
echo [%date% %time%] weflow-core has failed %WE_FAILS% times - it cannot open the WeChat DB.
echo [%date% %time%]   details: %USERPROFILE%\.weflow-core\logs\wcdb.log
echo [%date% %time%]   backing off 5 minutes before the next attempt.
timeout /t 300 >nul
set WE_FAILS=0
goto fe_ready

:fe_start_sender_check
if %W9999% neq 0 goto fe_start_sender
goto fe_ready

:fe_start_sender
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'frontend_sender\.bat' }) { exit 0 } else { exit 1 }"
if not errorlevel 1 (
    echo [%date% %time%] sender-service :9999 not up - a frontend loop already exists, not starting another.
    goto fe_ready
)
echo [%date% %time%] sender-service :9999 not up - starting it...
start "FE-sender-service" /min cmd /c C:\GSMPSWechatBot\frontend_sender.bat
goto fe_ready

:fe_ready
if exist run.log (for %%A in (run.log) do if %%~zA GTR 52428800 move /y run.log run_old.log >nul)
echo [%date% %time%] Starting bot...
rem Tee output: live console + run.log on disk
powershell -NoProfile -ExecutionPolicy Bypass -File C:\GSMPSWechatBot\run_bot.ps1
if %errorlevel%==2 (
    echo [%date% %time%] Another bot instance is already running ^(mutex^). This supervisor exits.
    pause
    exit /b 2
)
echo [%date% %time%] Bot exited. Restarting in 10s... (close this window to stop)
timeout /t 10 >nul
goto loop
