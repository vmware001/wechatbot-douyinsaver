@echo off
rem Restart only the frontend sender-service (picks up new code / new audio devices).
rem Kills the sender_service.py process; the frontend_sender.bat watcher loop
rem (running in a wt tab) auto-restarts it within 5s. If the loop is gone, opens a new one.
rem Auto-elevates: the sender runs as admin.

net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

chcp 65001 >nul
echo Stopping sender-service python...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'sender_service\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

timeout /t 2 >nul
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'frontend_sender\.bat' }) { exit 0 } else { exit 1 }"
if %errorlevel%==0 (
    echo Watcher loop is alive - it will restart sender-service automatically.
) else (
    echo Watcher loop not found, starting a new one...
    where wt.exe >nul 2>&1
    if %errorlevel%==0 (
        start "FE-sender-service" wt.exe --title "FE-sender:9999" --suppressApplicationTitle cmd /c C:\GSMPSWechatBot\frontend_sender.bat
    ) else (
        start "FE-sender-service" cmd /c C:\GSMPSWechatBot\frontend_sender.bat
    )
)
echo Done. This window can be closed.
timeout /t 3 >nul
exit /b
