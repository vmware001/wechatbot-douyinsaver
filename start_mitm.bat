@echo off
rem Start mitmdump capture for douyin traffic + enable system proxy.
rem Self-healing: restarts mitmdump if it exits. Stop with stop_mitm.bat.
title MITM-Douyin-Capture
cd /d C:\GSMPSWechatBot

rem single-instance guard: a duplicate launcher must exit instead of
rem spawning a second mitmdump that can never bind the port
netstat -ano | findstr "0.0.0.0:8080" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo mitmdump already listening on 8080 - duplicate launcher exits.
    exit /b 0
)

rem enable system proxy (idempotent)
powershell -NoProfile -Command "Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyEnable -Value 1; Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyServer -Value '127.0.0.1:8080'; Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyOverride -Value 'localhost;127.*;<local>'"
RUNDLL32.EXE USER32.DLL,UpdatePerUserSystemParameters 1,True

:loop
".venv\Scripts\mitmdump.exe" -p 8080 --set confdir=C:/GSMPSWechatBot/data/douyin_mitm/conf -s tools\douyin_mitm_addon.py --ignore-hosts "^(?!.*(douyin\.com|amemv|snssdk|zijieapi|bytedance)).*$" --set stream_large_bodies=5m --set connection_strategy=lazy >> data\douyin_mitm\mitmdump.log 2>&1
echo [%date% %time%] mitmdump exited, restarting in 3s >> data\douyin_mitm\mitmdump.log
ping -n 4 127.0.0.1 >nul
goto loop
