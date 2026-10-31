@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

rem 优先使用本目录的 venv；没有时回退到原 Bili2Text 安装的 venv
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    if exist "..\bili2text\.venv\Scripts\python.exe" (
        set "PY=..\bili2text\.venv\Scripts\python.exe"
    ) else (
        echo [ERROR] venv python not found. 请先安装依赖: pip install -r requirements.txt
        goto done
    )
)

if "%~1"=="" (
    "%PY%" "tools\glm_refine.py"
) else (
    "%PY%" "tools\glm_refine.py" %*
)

:done
echo.
pause
