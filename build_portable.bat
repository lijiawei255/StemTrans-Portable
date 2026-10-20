@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title StemTrans-Portable - Build portable folder

rem ===========================================================================
rem  Build a green/portable Windows folder with PyInstaller.
rem
rem    build_portable.bat
rem
rem  Output: dist\StemTrans-Portable\
rem    StemTrans-Portable.exe
rem    _internal\  config\  assets\  models\  licenses\
rem
rem  All required models are included (adds ~1.3 GB).
rem  ASCII-only on purpose (cmd.exe + UTF-8 batch files do not mix reliably).
rem ===========================================================================

set "ENV_NAME=stemtrans"
set "APP_NAME=StemTrans-Portable"
set "NOPAUSE=0"
if /I "%~1"=="/nopause" set "NOPAUSE=1"

set "CONDA_EXE="
for /f "delims=" %%I in ('where conda.exe 2^>nul') do (
    if not defined CONDA_EXE set "CONDA_EXE=%%I"
)
if not defined CONDA_EXE if exist "%USERPROFILE%\anaconda3\Scripts\conda.exe" set "CONDA_EXE=%USERPROFILE%\anaconda3\Scripts\conda.exe"
if not defined CONDA_EXE if exist "%USERPROFILE%\miniconda3\Scripts\conda.exe" set "CONDA_EXE=%USERPROFILE%\miniconda3\Scripts\conda.exe"
if not defined CONDA_EXE if exist "%ProgramData%\Anaconda3\Scripts\conda.exe" set "CONDA_EXE=%ProgramData%\Anaconda3\Scripts\conda.exe"

if not defined CONDA_EXE (
    echo [ERROR] conda.exe not found.
    goto fail
)
for %%I in ("%CONDA_EXE%") do set "CONDA_ROOT=%%~dpI.."
for %%I in ("%CONDA_ROOT%") do set "CONDA_ROOT=%%~fI"

"%CONDA_EXE%" env list 2>nul | findstr /B /C:"%ENV_NAME% " >nul 2>nul
if errorlevel 1 (
    echo [ERROR] environment "%ENV_NAME%" missing. Run setup_conda_env.bat.
    goto fail
)
call "%CONDA_ROOT%\Scripts\activate.bat" %ENV_NAME%
if errorlevel 1 goto fail

echo.
echo ===========================================================================
echo   Build portable folder: %APP_NAME%
echo ===========================================================================
echo.

echo Building complete portable distribution, including models ...
python -m pip install "pyinstaller>=6.3,<7"
if errorlevel 1 goto fail
python tools\build_portable.py
if errorlevel 1 goto fail
echo.
echo Portable folder: %CD%\dist\StemTrans-Portable
if "%NOPAUSE%"=="0" pause
exit /b 0

:fail
echo.
echo [ERROR] build aborted.
if "%NOPAUSE%"=="0" pause
exit /b 1
