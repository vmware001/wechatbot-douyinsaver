@echo off
rem Douyin pipeline autostart: ONE Windows Terminal window, one tab per
rem component (mitm / saver / forwarder / console / scroll watch).
rem mitm sits in tab 1 and binds 8080 within seconds; every other component
rem retries on its own, so boot ordering needs no hard waits.
rem Fallback when wt.exe is missing: legacy minimized windows.
cd /d C:\GSMPSWechatBot

rem double-launch guard: if the stack is already up (mitm + console answer),
rem exit instead of opening a second window with duplicate tabs
netstat -ano | findstr "0.0.0.0:8080" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    curl -s -m 3 -o nul http://127.0.0.1:9998/api/status
    if %errorlevel%==0 (
        echo douyin stack already running - duplicate launch exits.
        exit /b 0
    )
)

if defined WT_SESSION goto legacy
where wt.exe >nul 2>&1
if not errorlevel 1 goto wtmode
goto legacy

:wtmode
wt -w new --title Douyin-MITM cmd /c C:\GSMPSWechatBot\start_mitm.bat ; new-tab --title DouyinSaver cmd /c C:\GSMPSWechatBot\_svc_loop.bat DouyinSaver C:\GSMPSWechatBot\.venv\Scripts\python.exe -u C:\GSMPSWechatBot\tools\douyin_saver.py ; new-tab --title DouyinForwarder cmd /c C:\GSMPSWechatBot\_svc_loop.bat DouyinForwarder C:\GSMPSWechatBot\.venv\Scripts\python.exe -u C:\GSMPSWechatBot\tools\douyin_forwarder.py ; new-tab --title DouyinConsole cmd /c C:\GSMPSWechatBot\_svc_loop.bat DouyinConsole C:\GSMPSWechatBot\.venv\Scripts\python.exe -u C:\GSMPSWechatBot\tools\douyin_console.py ; new-tab --title DouyinScrollWatch cmd /c C:\GSMPSWechatBot\_svc_loop.bat DouyinScrollWatch C:\GSMPSWechatBot\.venv\Scripts\python.exe -u C:\GSMPSWechatBot\tools\douyin_scroll_watch.py ; new-tab --title CloudflareTunnel cmd /c C:\GSMPSWechatBot\_svc_loop.bat CloudflareTunnel C:\GSMPSWechatBot\.venv\Scripts\python.exe -u C:\GSMPSWechatBot\tools\cloudflared_run.py
ping -n 8 127.0.0.1 >nul
call start_douyin_chrome.bat
exit /b

:legacy
start "MITM-Douyin-Capture" /min cmd /c start_mitm.bat
ping -n 8 127.0.0.1 >nul
call start_douyin_chrome.bat
ping -n 20 127.0.0.1 >nul
call start_douyin_save.bat
call start_douyin_forward.bat
call start_douyin_console.bat
start "DouyinScrollWatch" /min cmd /c _svc_loop.bat DouyinScrollWatch .venv\Scripts\python.exe -u tools\douyin_scroll_watch.py
start "CloudflareTunnel" /min cmd /c _svc_loop.bat CloudflareTunnel .venv\Scripts\python.exe -u tools\cloudflared_run.py
