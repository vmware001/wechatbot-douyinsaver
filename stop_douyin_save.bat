@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*douyin_saver*' } | ForEach-Object { Write-Output ('killing ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }"
