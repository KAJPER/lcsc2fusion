@echo off
rem Double click to remove the LCSC2Fusion add-in from Autodesk Fusion.
rem This file only starts uninstall.ps1 from its own folder. Both are plain text: read them first.
rem To also delete your settings and state, run: Uninstall-LCSC2Fusion.bat -RemoveData
rem Dwuklik usuwa dodatek LCSC2Fusion. Ten plik tylko uruchamia uninstall.ps1.
setlocal
if not exist "%~dp0uninstall.ps1" (
    echo uninstall.ps1 was not found next to this file. Extract the whole zip first, then run this file again.
    echo Nie znaleziono uninstall.ps1 obok tego pliku. Najpierw rozpakuj caly plik zip, potem uruchom ten plik ponownie.
    echo.
    pause
    exit /b 10
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
