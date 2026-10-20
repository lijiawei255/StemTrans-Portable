@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title StemTrans-Portable

rem ===========================================================================
rem  Daily launcher.
rem  Double-click this file, or:  run_app.bat --self-test
rem  ASCII-only on purpose (cmd.exe + UTF-8 batch files do not mix reliably).
rem ===========================================================================

set "ENV_NAME=stemtrans"
set "CONDA_EXE="
for /f "delims=" %%I in ('where conda.exe 2^>nul') do (
    if not defined CONDA_EXE set "CONDA_EXE=%%I"
)
if not defined CONDA_EXE if exist "%USERPROFILE%\anaconda3\Scripts\conda.exe" set "CONDA_EXE=%USERPROFILE%\anaconda3\Scripts\conda.exe"
if not defined CONDA_EXE if exist "%USERPROFILE%\miniconda3\Scripts\conda.exe" set "CONDA_EXE=%USERPROFILE%\miniconda3\Scripts\conda.exe"
if not defined CONDA_EXE if exist "%ProgramData%\Anaconda3\Scripts\conda.exe" set "CONDA_EXE=%ProgramData%\Anaconda3\Scripts\conda.exe"

if not defined CONDA_EXE (
    echo [ERROR] conda.exe not found. Install Anaconda or add it to PATH.
    pause
    exit /b 1
)

for %%I in ("%CONDA_EXE%") do set "CONDA_ROOT=%%~dpI.."
for %%I in ("%CONDA_ROOT%") do set "CONDA_ROOT=%%~fI"

"%CONDA_EXE%" env list 2>nul | findstr /B /C:"%ENV_NAME% " >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] Conda environment "%ENV_NAME%" does not exist.
    echo         Run setup_conda_env.bat first.
    echo.
    pause
    exit /b 1
)

call "%CONDA_ROOT%\Scripts\activate.bat" %ENV_NAME%
if errorlevel 1 (
    echo [ERROR] cannot activate "%ENV_NAME%".
    pause
    exit /b 1
)

python -c "import customtkinter, numpy, pyaudiowpatch" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] The environment is incomplete.
    echo         Run setup_conda_env.bat again.
    echo.
    pause
    exit /b 1
)

echo.
echo  StemTrans-Portable  ^|  local CPU interpreter
echo  Close the window to exit completely (no tray, no background process).
echo.

python main.py %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo [exit %EXITCODE%] See logs\app.log for details.
    pause
)
exit /b %EXITCODE%
