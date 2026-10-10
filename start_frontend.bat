@echo off
rem Start frontend services only: weflow-core (:5031) + sender-service (:9999)
rem Auto-elevates because WeChat runs as admin (UIPI blocks normal-permission UIA).

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
title GSMPS Frontend Launcher

echo Starting frontend services...
where wt.exe >nul 2>&1
if %errorlevel%==0 (
    wt -w 0 new-tab --title "FE-weflow:5031" --suppressApplicationTitle cmd /c C:\GSMPSWechatBot\frontend_weflow.bat
    wt -w 0 new-tab --title "FE-sender:9999" --suppressApplicationTitle cmd /c C:\GSMPSWechatBot\frontend_sender.bat
) else (
    start "FE-weflow-core" cmd /c C:\GSMPSWechatBot\frontend_weflow.bat
    start "FE-sender-service" cmd /c C:\GSMPSWechatBot\frontend_sender.bat
)
echo.
echo Frontend started in two windows:
echo   FE-weflow-core     http://127.0.0.1:5031/health
echo   FE-sender-service  http://127.0.0.1:9999/health
echo.
echo This launcher window can be closed.
pause
exit /b
