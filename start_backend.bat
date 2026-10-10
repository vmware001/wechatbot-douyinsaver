@echo off
rem Start backend only (bot brain). Requires frontend already running
rem (main.py waits for :5031 and :9999 health and exits with a hint otherwise).
call C:\GSMPSWechatBot\start_bot_vm.bat
