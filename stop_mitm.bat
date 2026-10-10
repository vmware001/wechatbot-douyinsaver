@echo off
rem Stop the mitm capture loop and restore direct networking.
powershell -NoProfile -Command "Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyEnable -Value 0"
RUNDLL32.EXE USER32.DLL,UpdatePerUserSystemParameters 1,True
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'mitmdump' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
taskkill /FI "WINDOWTITLE eq MITM-Douyin-Capture" /F >nul 2>&1
echo mitm stopped, system proxy disabled.
