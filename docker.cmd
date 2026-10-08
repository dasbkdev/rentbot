@echo off
rem Forward Docker CLI commands to the local Ubuntu WSL engine.
rem WSL inherits this terminal's current working directory. Use Linux-style paths in arguments.+powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0wsl-keepalive.ps1"
if errorlevel 1 exit /b %errorlevel%
wsl.exe --distribution Ubuntu-24.04 --exec docker %*
exit /b %errorlevel%
