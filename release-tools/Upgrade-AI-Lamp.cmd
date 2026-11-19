@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Upgrade-AI-Lamp.ps1" %*
if errorlevel 1 echo Upgrade incomplete. Read the message above.
pause
