@echo off
rem Used inside LCSC2Fusion-Setup-<version>.exe (built by scripts/build_release.py with IExpress).
rem The exe unpacks installer.zip and this file into a temporary folder and runs this file there.
rem   bootstrap.cmd          normal run: install.ps1 -Gui, the result is shown in a message box
rem   bootstrap.cmd quiet    no windows at all: install.ps1 -Silent, only the exit code
rem The installer output goes to %TEMP%\LCSC2Fusion-setup.log (LCSC2FUSION_SETUP_LOG overrides it).
setlocal
set "MODE=-Gui"
if /i "%~1"=="quiet" set "MODE=-Silent"
set "LCSC_ZIP=%~dp0installer.zip"
set "LCSC_WORK=%TEMP%\LCSC2Fusion-setup-%RANDOM%-%RANDOM%"
set "LCSC_LOG=%TEMP%\LCSC2Fusion-setup.log"
if defined LCSC2FUSION_SETUP_LOG set "LCSC_LOG=%LCSC2FUSION_SETUP_LOG%"
set "LCSC_CLEAN="
set "RC=14"

if exist "%LCSC_WORK%" goto cleanup
mkdir "%LCSC_WORK%"
if errorlevel 1 goto cleanup
set "LCSC_CLEAN=1"

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try { Expand-Archive -LiteralPath $env:LCSC_ZIP -DestinationPath $env:LCSC_WORK -Force } catch { Write-Host $_.Exception.Message; exit 1 }" > "%LCSC_LOG%" 2>&1
if errorlevel 1 goto cleanup
if not exist "%LCSC_WORK%\install.ps1" goto cleanup

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LCSC_WORK%\install.ps1" %MODE% >> "%LCSC_LOG%" 2>&1
set "RC=%ERRORLEVEL%"

:cleanup
cd /d "%TEMP%"
if defined LCSC_CLEAN rmdir /s /q "%LCSC_WORK%"
exit /b %RC%
