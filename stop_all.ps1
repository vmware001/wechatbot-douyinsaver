# stop_all.ps1 - kill backend (bot brain + supervisors) and frontend (weflow-core + sender-service)
$ErrorActionPreference = 'SilentlyContinue'

# 排除自身及祖先进程（防止 start_all.bat 调用本脚本时把自己所在的 cmd/wt 一起杀掉）
$exclude = @($PID)
$cur = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
while ($cur -and $cur.ParentProcessId) {
    $exclude += $cur.ParentProcessId
    $cur = Get-CimInstance Win32_Process -Filter "ProcessId=$($cur.ParentProcessId)"
}

$patterns = 'main\.py', 'sender_service\.py', 'run_bot\.ps1', 'start_bot_vm\.bat', 'start_all\.bat', 'start_backend\.bat', 'frontend_weflow\.bat', 'frontend_sender\.bat', 'fe_keepalive\.py'
$regex = ($patterns -join '|')

$targets = Get-CimInstance Win32_Process | Where-Object {
    $exclude -notcontains $_.ProcessId -and $_.CommandLine -and $_.CommandLine -match $regex
}

foreach ($p in $targets) {
    Write-Output ("kill PID={0}  {1}" -f $p.ProcessId, ($p.CommandLine -replace '\s+', ' ').Substring(0, [Math]::Min(100, $p.CommandLine.Length)))
    Stop-Process -Id $p.ProcessId -Force
}

# weflow-core runs as WeFlow.exe (renamed node.exe)
$weflow = Get-Process -Name 'WeFlow' -ErrorAction SilentlyContinue
foreach ($p in $weflow) {
    Write-Output ("kill PID={0}  WeFlow.exe (weflow-core)" -f $p.Id)
    Stop-Process -Id $p.Id -Force
}

if (-not $targets -and -not $weflow) {
    Write-Output 'no bot processes found'
}
