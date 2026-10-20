@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title StemTrans-Portable - One-click Setup

rem ===========================================================================
rem  StemTrans-Portable  /  Anaconda environment bootstrap (Windows cmd)
rem
rem    setup_conda_env.bat              interactive, China mirror by default
rem    setup_conda_env.bat /check       verify only, install nothing
rem    setup_conda_env.bat /yes         no prompts, China mirror, full install
rem    setup_conda_env.bat /skip-llm    continue without llama-cpp-python
rem    setup_conda_env.bat /no-capture  skip the README screenshot
rem    setup_conda_env.bat /skip-models skip the 1.3 GB model download
rem
rem  NOTE: this file is intentionally ASCII-only. Windows cmd.exe corrupts
rem  UTF-8 batch files when a multi-byte character straddles a read buffer,
rem  which produces "not recognized as an internal command" errors.
rem ===========================================================================

set "ENV_NAME=stemtrans"
set "PY_VER=3.10"
set "MODE=install"
set "ASSUME=0"
set "DO_CAPTURE=1"
set "DO_MODELS=1"
set "SKIP_LLM=0"
set "NOPAUSE=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="/check" set "MODE=check"
if /I "%~1"=="/yes" set "ASSUME=1"
if /I "%~1"=="/no-capture" set "DO_CAPTURE=0"
if /I "%~1"=="/skip-models" set "DO_MODELS=0"
if /I "%~1"=="/skip-llm" set "SKIP_LLM=1"
if /I "%~1"=="/nopause" set "NOPAUSE=1"
shift
goto parse_args
:args_done

echo.
echo ===========================================================================
echo   StemTrans-Portable  ^|  Environment Setup
echo   Conda env : %ENV_NAME%   Python %PY_VER%   (base is never modified)
echo   llama.cpp : official pre-built CPU wheel, zero MSVC compile
echo ===========================================================================
echo.

rem ---------------------------------------------------------------------------
rem 1. locate conda
rem ---------------------------------------------------------------------------
set "CONDA_EXE="
for /f "delims=" %%I in ('where conda.exe 2^>nul') do (
    if not defined CONDA_EXE set "CONDA_EXE=%%I"
)
if not defined CONDA_EXE if exist "%USERPROFILE%\anaconda3\Scripts\conda.exe" set "CONDA_EXE=%USERPROFILE%\anaconda3\Scripts\conda.exe"
if not defined CONDA_EXE if exist "%USERPROFILE%\miniconda3\Scripts\conda.exe" set "CONDA_EXE=%USERPROFILE%\miniconda3\Scripts\conda.exe"
if not defined CONDA_EXE if exist "%ProgramData%\Anaconda3\Scripts\conda.exe" set "CONDA_EXE=%ProgramData%\Anaconda3\Scripts\conda.exe"
if not defined CONDA_EXE if exist "%ProgramData%\miniconda3\Scripts\conda.exe" set "CONDA_EXE=%ProgramData%\miniconda3\Scripts\conda.exe"

if not defined CONDA_EXE (
    echo [ERROR] Anaconda / Miniconda not found.
    echo         Install Anaconda, enable "Add to PATH", or add the
    echo         folder that contains conda.exe to PATH.
    goto fail
)

for %%I in ("%CONDA_EXE%") do set "CONDA_ROOT=%%~dpI.."
for %%I in ("%CONDA_ROOT%") do set "CONDA_ROOT=%%~fI"
echo [ok] conda : "%CONDA_EXE%"
echo [ok] root  : "%CONDA_ROOT%"

rem ---------------------------------------------------------------------------
rem 2. environment presence
rem ---------------------------------------------------------------------------
set "ENV_EXISTS=0"
"%CONDA_EXE%" env list 2>nul | findstr /B /C:"%ENV_NAME% " >nul 2>nul
if not errorlevel 1 set "ENV_EXISTS=1"

if "%MODE%"=="check" (
    echo.
    echo ------------------------------- CHECK ---------------------------------
    echo   conda      : "%CONDA_EXE%"
    echo   env name   : %ENV_NAME%
    if "%ENV_EXISTS%"=="1" (echo   env status : EXISTS) else (echo   env status : MISSING)
    if exist "requirements.txt" (echo   requirements: OK) else (echo   requirements: MISSING)
    if exist "main.py" (echo   entrypoint  : OK) else (echo   entrypoint  : MISSING)
    if not exist "models\silero_vad.onnx" echo   vad model   : MISSING
    if not exist "models\qwen2.5-1.5b-instruct-q4_k_m.gguf" echo   llm model   : MISSING
    if not exist "models\whisper-base\model.bin" echo   asr model   : MISSING
    echo -----------------------------------------------------------------------
    echo.
    if "%NOPAUSE%"=="0" pause
    exit /b 0
)

rem ---------------------------------------------------------------------------
rem 3. mirror
rem ---------------------------------------------------------------------------
if "%ASSUME%"=="1" goto mirror_china

echo.
echo Choose a dependency mirror:
echo   [1] China    - Tsinghua Tuna PyPI + Tuna conda-forge  (recommended in CN)
echo   [2] Global   - pypi.org + conda-forge
echo.
set "CHOICE="
set /p "CHOICE=  Enter 1 or 2 [default 1]: "
if "!CHOICE!"=="2" goto mirror_global

:mirror_china
set "SOURCE=china"
set "PIP_INDEX=-i https://pypi.tuna.tsinghua.edu.cn/simple"
set "CONDA_CH=--override-channels -c https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge -c https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main"
goto mirror_done

:mirror_global
set "SOURCE=global"
set "PIP_INDEX="
set "CONDA_CH=-c conda-forge"
goto mirror_done

:mirror_done
echo [ok] source: %SOURCE%

rem ---------------------------------------------------------------------------
rem 4. create + activate
rem ---------------------------------------------------------------------------
echo.
echo [1/4] Conda environment
if "%ENV_EXISTS%"=="1" goto env_ready

echo       creating %ENV_NAME% ...
"%CONDA_EXE%" create -y -n %ENV_NAME% %CONDA_CH% python=%PY_VER% pip
if errorlevel 1 (
    echo       [warn] mirror failed, retrying with official conda-forge ...
    "%CONDA_EXE%" create -y -n %ENV_NAME% -c conda-forge python=%PY_VER% pip
)
if errorlevel 1 (
    echo [ERROR] conda create failed.
    goto fail
)
goto env_activate

:env_ready
echo       %ENV_NAME% already exists, skipping creation.

:env_activate
call "%CONDA_ROOT%\Scripts\activate.bat" %ENV_NAME%
if errorlevel 1 (
    echo [ERROR] cannot activate %ENV_NAME%.
    goto fail
)
python -c "import sys; print('      python: ' + sys.executable + '  (' + sys.version.split()[0] + ')')"
if errorlevel 1 goto fail

rem ---------------------------------------------------------------------------
rem 5. pip + llama-cpp-python CPU wheel + requirements
rem ---------------------------------------------------------------------------
echo.
echo [2/4] pip, setuptools, llama-cpp-python CPU wheel
python -m pip install --upgrade pip setuptools wheel !PIP_INDEX!
if errorlevel 1 (
    echo       [warn] mirror failed for pip upgrade, using official PyPI ...
    python -m pip install --upgrade pip setuptools wheel
)

python -m pip install "llama-cpp-python>=0.3.8,<0.4" --only-binary=:all: --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu !PIP_INDEX!
if errorlevel 1 (
    echo.
    echo       [warn] pre-built wheel installation failed. Possible reasons:
    echo              - Python is not 3.10 / 3.11 / 3.12
    echo              - abetlen.github.io is unreachable
    echo              - no CPU wheel exists for this platform
    if "%SKIP_LLM%"=="1" goto llm_skipped
    echo       Re-run with /skip-llm to continue without the local LLM.
    goto fail
)
:llm_skipped

echo.
echo [3/4] Python packages
if "%SKIP_LLM%"=="1" (
    if not exist "logs" mkdir "logs"
    findstr /V /I "llama-cpp-python" requirements.txt > "logs\requirements_no_llm.txt"
    set "REQ_FILE=logs\requirements_no_llm.txt"
) else (
    set "REQ_FILE=requirements.txt"
)
python -m pip install -r "!REQ_FILE!" !PIP_INDEX! --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
if errorlevel 1 (
    echo       [warn] mirror failed, retrying with official PyPI ...
    python -m pip install -r "!REQ_FILE!" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
)
if errorlevel 1 (
    echo [ERROR] package installation failed.
    goto fail
)

echo.
echo       verifying imports ...
python -c "import numpy, onnxruntime, faster_whisper, pyaudiowpatch, customtkinter, PIL, pyttsx3; print('      core deps OK')"
if errorlevel 1 (
    echo [ERROR] core dependencies failed to import.
    goto fail
)
if "%SKIP_LLM%"=="0" (
    python -c "import llama_cpp; print('      llama_cpp OK')"
    if errorlevel 1 (
        echo [ERROR] llama_cpp failed to import.
        goto fail
    )
)

rem ---------------------------------------------------------------------------
rem 6. models
rem ---------------------------------------------------------------------------
echo.
echo [4/4] Local models
if "%DO_MODELS%"=="0" (
    echo       skipped by /skip-models
    goto models_done
)
if "%ASSUME%"=="1" goto download_models

set "DL="
set /p "DL=  Download models now? about 1.3 GB, resumable [Y/n]: "
if /I "!DL!"=="n" (
    echo       skipped. Run later: python download_models.py
    goto models_done
)

:download_models
python download_models.py --source %SOURCE% --whisper base --all
:models_done

rem ---------------------------------------------------------------------------
rem 7. real window screenshot
rem ---------------------------------------------------------------------------
if "%DO_CAPTURE%"=="0" goto capture_done
echo.
echo [extra] capturing the real window -^> assets\ui_live_capture.png
python capture_readme_screenshots.py
if errorlevel 1 (
    echo        [warn] capture failed: no interactive desktop or window occluded.
    echo        Run it manually from a normal desktop session:
    echo           python capture_readme_screenshots.py
)
:capture_done

echo.
echo ===========================================================================
echo   Setup complete.
echo.
echo   Daily launch : double-click run_app.bat
echo   Self-test    : run_app.bat --self-test
echo   Environment  : conda activate %ENV_NAME%
echo ===========================================================================
echo.
if "%NOPAUSE%"=="0" pause
exit /b 0

:fail
echo.
echo ===========================================================================
echo   Setup did not complete. Fix the message above and run again.
echo ===========================================================================
echo.
if "%NOPAUSE%"=="0" pause
exit /b 1
