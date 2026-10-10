Get-ScheduledTask | Where-Object { $_.TaskName -match 'wechat|bot' } | ForEach-Object {
    $a = (Get-ScheduledTaskInfo $_.TaskName -ErrorAction SilentlyContinue)
    $act = ($_ | Get-ScheduledTaskInfo) | Out-Null
    $detail = (Get-ScheduledTask -TaskName $_.TaskName).Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments }
    Write-Output ($_.TaskName + ' | ' + $_.State + ' | ' + ($detail -join '; '))
}
