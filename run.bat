@echo off
cd /d "%~dp0"

rem No URL needed: the dashboard has a URL bar.
rem You can still pass one:  run.bat "https://youtube.com/watch?v=..." --no-open
if "%~1"=="" (
  python clipper.py
) else (
  python clipper.py "%~1" %2 %3 %4 %5 %6 %7 %8 %9
)

echo.
pause
