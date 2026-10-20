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
rem    config\  assets\  models\  README.md  LICENSE
rem
rem  Models are copied only if you answer Y (adds ~1.3 GB).
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

python -m PyInstaller --version >nul 2>nul
echo [1/5] ensuring PyInstaller ^>= 6.3 ...
python -m pip install --upgrade "pyinstaller>=6.3"
if errorlevel 1 goto fail

echo [2/5] cleaning previous build ...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

echo [3/5] running PyInstaller (this can take several minutes) ...
python -m PyInstaller ^
    --noconfirm --clean --windowed --onedir ^
    --contents-directory "." ^
    --name "%APP_NAME%" ^
    --add-data "config\settings.json;config" ^
    --add-data "config\glossary.json;config" ^
    --add-data "assets;assets" ^
    --collect-all customtkinter ^
    --collect-all llama_cpp ^
    --collect-all faster_whisper ^
    --collect-all ctranslate2 ^
    --collect-all onnxruntime ^
    --collect-all pyaudiowpatch ^
    --collect-submodules pyttsx3 ^
    --collect-submodules comtypes ^
    --collect-data zhconv ^
    --hidden-import core.audio_capture ^
    --hidden-import core.inference_engine ^
    --hidden-import core.tts_worker ^
    --hidden-import ui.main_window ^
    main.py
if errorlevel 1 (
    echo [ERROR] PyInstaller failed.
    goto fail
)

set "OUT=dist\%APP_NAME%"
if not exist "%OUT%\%APP_NAME%.exe" (
    echo [ERROR] expected exe not found: %OUT%\%APP_NAME%.exe
    goto fail
)

echo [4/5] copying docs and model folder ...
copy /Y "README.md" "%OUT%\README.md" >nul
copy /Y "LICENSE" "%OUT%\LICENSE" >nul
if not exist "%OUT%\models" mkdir "%OUT%\models"
if exist "models\silero_vad.onnx" copy /Y "models\silero_vad.onnx" "%OUT%\models\silero_vad.onnx" >nul
if exist "models\whisper-base\model.bin" (
    xcopy /E /I /Y "models\whisper-base" "%OUT%\models\whisper-base" >nul
)
for %%G in ("models\*.gguf") do (
    if exist "%%~fG" copy /Y "%%~fG" "%OUT%\models\" >nul
)

set "COPY_MODELS="
set /p "COPY_MODELS=Copy the full models folder too? (~1.3 GB) [y/N]: "
if /I "!COPY_MODELS!"=="y" (
    echo       copying models ...
    robocopy "models" "%OUT%\models" /E /XD hf_cache torch_cache xdg_cache numba_cache /NFL /NDL /NJH /NJS >nul
)

echo [5/5] done.
echo.
for /f %%S in ('powershell -NoProfile -Command "[math]::Round((Get-ChildItem -Recurse -File '%OUT%' ^| Measure-Object Length -Sum).Sum/1MB,1)"') do set "SIZE=%%S"
echo   folder : %CD%\%OUT%
echo   size   : %SIZE% MB
echo.
echo   Double-click %APP_NAME%.exe inside that folder.
echo   The folder is self-contained: copy it to a USB stick and run.
echo.
if "%NOPAUSE%"=="0" pause
exit /b 0

:fail
echo.
echo [ERROR] build aborted.
if "%NOPAUSE%"=="0" pause
exit /b 1
