@echo off
rem Double-click to open the live P&L dashboard for the Kalshi/Polymarket BTC 15-minute PAPER bot. It places NO orders.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m pmarb ui %*
) else (
  python -m pmarb ui %*
)
if errorlevel 1 (
  echo.
  echo Could not start. Install Python 3.10 or newer from python.org and tick "Add python.exe to PATH".
  pause
)
