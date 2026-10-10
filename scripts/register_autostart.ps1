$action = New-ScheduledTaskAction -Execute 'C:\GSMPSWechatBot\start_bot_vm.bat'
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = 'PT1M'
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'GSMPSWechatBot' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -Description 'GSMPS Wechat Bot supervisor (start_bot_vm.bat), delayed 1min after logon so WeChat starts first'
Write-Output '--- created ---'
Get-ScheduledTask -TaskName 'GSMPSWechatBot' | Format-List TaskName, State
