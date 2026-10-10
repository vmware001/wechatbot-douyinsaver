taskkill /F /PID 6484 2>$null
taskkill /F /IM python.exe 2>$null
taskkill /F /IM WeFlow.exe 2>$null
Start-Sleep -Seconds 2
Start-Process wt.exe -Verb RunAs -ArgumentList '--','cmd','/c','C:\GSMPSWechatBot\start_bot_vm.bat'
