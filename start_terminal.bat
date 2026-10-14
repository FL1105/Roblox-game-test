@echo off
rem Double-click to open the paper-trading terminal with a SIMULATED demo market.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m sniper.terminal %*
) else (
  python -m sniper.terminal %*
)
if errorlevel 1 (
  echo.
  echo Could not start. Install Python 3.10 or newer from python.org and tick "Add python.exe to PATH".
  pause
)
