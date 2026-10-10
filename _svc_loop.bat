@echo off
rem Generic supervised tab: restart the given command whenever it exits.
rem Usage: _svc_loop.bat <title> <command...>  (max 8 command args, no quotes/spaces in args)
rem NOTE: use ping instead of timeout - PATH may contain Git's GNU timeout.
title %~1
set CMD=%2 %3 %4 %5 %6 %7 %8 %9
:loop
echo [%date% %time%] starting: %CMD%
%CMD%
echo [%date% %time%] exited (code %errorlevel%), restarting in 5s...
ping -n 6 127.0.0.1 >nul
goto loop
