@echo off
rem Double click to install the LCSC2Fusion add-in for Autodesk Fusion.
rem This file only starts install.ps1 from its own folder. Both are plain text: read them first.
rem Dwuklik instaluje dodatek LCSC2Fusion dla Autodesk Fusion. Ten plik tylko uruchamia install.ps1.
setlocal
if not exist "%~dp0install.ps1" (
    echo install.ps1 was not found next to this file. Extract the whole zip first, then run this file again.
    echo Nie znaleziono install.ps1 obok tego pliku. Najpierw rozpakuj caly plik zip, potem uruchom ten plik ponownie.
    echo.
    pause
    exit /b 10
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
