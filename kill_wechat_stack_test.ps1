# Elevated cleanup for the wechat-bot stack (FE services run as admin).
# Kills sender-service / weflow-core / bot brain leftovers. Never touches Weixin.
$patterns = 'sender_service', 'frontend_sender', 'frontend_weflow', 'WeFlow', 'start_bot_vm', 'run_bot', 'main\.py'
$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and ($_.CommandLine -match ($patterns -join '|')) -and $_.CommandLine -notmatch 'kill_wechat_stack'
}
foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
# also the elevated wt window hosting BE-brain tabs (title match)
Get-Process WindowsTerminal -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowTitle -match 'BE-brain' } |
    ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
$alive = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and ($_.CommandLine -match ($patterns -join '|')) -and $_.CommandLine -notmatch 'kill_wechat_stack'
}
if ($alive) { "STILL ALIVE: $($alive.ProcessId -join ',')" } else { "CLEAN" }
