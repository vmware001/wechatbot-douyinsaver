@echo off
cd /d C:\GSMPSWechatBot
set PYTHONIOENCODING=utf-8
start "DouyinConsole" /min .venv\Scripts\python.exe tools\douyin_console.py
