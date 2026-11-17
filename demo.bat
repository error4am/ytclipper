@echo off
cd /d "%~dp0"
echo Starting the local demo stream - no YouTube needed.
echo Clips will start appearing in .\clips within a minute.
echo Close this window (or press Ctrl+C) to stop.
echo.
python clipper.py --test %*
pause
