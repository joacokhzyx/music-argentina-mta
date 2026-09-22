@echo off
set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
cd /d "%~dp0"
python server.py
pause
