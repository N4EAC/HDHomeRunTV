@echo off
setlocal
cd /d "%~dp0"
py -m pip install --upgrade pyinstaller pillow
py -m PyInstaller --noconfirm --clean --windowed --onefile ^
  --name "HDHomeRunTV" ^
  --icon "assets\hdhomerun_tv.ico" ^
  --add-data "assets\hdhomerun_tv.ico;assets" ^
  --add-data "assets\hdhomerun_tv.png;assets" ^
  hdhomerun_tv.py
if errorlevel 1 pause & exit /b 1
copy /y "dist\HDHomeRunTV.exe" ".\HDHomeRunTV.exe"
echo.
echo Build complete: HDHomeRunTV.exe
pause
