@echo off
rem Double-click to open the paper-trading terminal with a SIMULATED demo market.
cd /d "%~dp0"
python -m sniper.terminal %*
if errorlevel 1 pause
