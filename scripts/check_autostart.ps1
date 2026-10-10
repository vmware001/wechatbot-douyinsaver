$t = Get-ScheduledTask -TaskName 'GSMPSWechatBot'
Write-Output ('RunLevel: ' + $t.Principal.RunLevel)
Write-Output ('UserId: ' + $t.Principal.UserId)
Write-Output ('LogonType: ' + $t.Principal.LogonType)
Write-Output ('Delay: ' + $t.Triggers[0].Delay)
Write-Output ('ExecTimeLimit: ' + $t.Settings.ExecutionTimeLimit)
Write-Output ('State: ' + $t.State)
