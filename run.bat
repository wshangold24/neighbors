@echo off
REM Windows: double-click this file (or run it in Command Prompt) to launch the app.
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM aptfinder needs Python 3.9+. The default `python` might be too old even when a
REM newer one is installed, so prefer the Windows "py" launcher and ask it for a
REM specific version, newest-first. Fall back to py -3, then plain python.
set "PYCMD="
for %%V in (3.13 3.12 3.11 3.10 3.9) do (
  if not defined PYCMD (
    py -%%V -c "import sys" >nul 2>nul && set "PYCMD=py -%%V"
  )
)
if not defined PYCMD (
  py -3 -c "import sys; sys.exit(0 if sys.version_info[:2]>=(3,9) else 1)" >nul 2>nul && set "PYCMD=py -3"
)
if not defined PYCMD (
  python -c "import sys; sys.exit(0 if sys.version_info[:2]>=(3,9) else 1)" >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD (
  echo aptfinder needs Python 3.9 or newer, but no compatible version was found.
  echo Install one from https://www.python.org/downloads/
  echo Be sure to check "Add Python to PATH" during install, then try again.
  pause
  exit /b 1
)
for /f "delims=" %%v in ('%PYCMD% --version 2^>^&1') do echo Using %%v

REM Rebuild the environment if it was created with a now-incompatible Python.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python" -c "import sys; sys.exit(0 if sys.version_info[:2]>=(3,9) else 1)" >nul 2>nul || (
    echo Existing environment uses an unsupported Python - rebuilding it...
    rmdir /s /q .venv
  )
)

if not exist ".venv\Scripts\aptfinder.exe" (
  echo First-time setup: creating a private Python environment and installing aptfinder...
  %PYCMD% -m venv .venv || ( echo Could not create the environment. & pause & exit /b 1 )
  ".venv\Scripts\python" -m pip install --quiet --upgrade pip
  ".venv\Scripts\pip" install --quiet . || ( echo Install failed. & pause & exit /b 1 )
)

echo Starting aptfinder - your browser will open shortly. Close this window to stop.
".venv\Scripts\aptfinder" web
pause
