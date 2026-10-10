@echo off
cd /d C:\GSMPSWechatBot
set PYTHONIOENCODING=utf-8
start "DouyinForwarder" /min .venv\Scripts\python.exe tools\douyin_forwarder.py
