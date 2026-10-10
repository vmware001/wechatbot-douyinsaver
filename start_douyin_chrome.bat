@echo off
REM Douyin debug Chrome (alternative to Edge): port 9223, own profile.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*chrome_douyin*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
ping -n 3 127.0.0.1 >nul
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9223 --user-data-dir=C:\GSMPSWechatBot\data\chrome_douyin --no-first-run --no-default-browser-check --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-tab-discarding --disable-features=HighEfficiencyMode,BatterySaverMode --new-window "https://www.douyin.com/chat"
