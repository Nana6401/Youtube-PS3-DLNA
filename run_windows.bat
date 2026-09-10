@echo off
setlocal
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail
python ps3_youtube_dlna.py
exit /b %errorlevel%
:fail
echo.
echo Could not install the Python dependency.
pause
exit /b 1
