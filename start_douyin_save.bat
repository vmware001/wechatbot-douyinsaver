@echo off
cd /d C:\GSMPSWechatBot
set PYTHONIOENCODING=utf-8
start "DouyinSaver" /min .venv\Scripts\python.exe tools\douyin_saver.py
