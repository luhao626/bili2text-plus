@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

rem Bili2Text Plus 启动器：优先本目录 venv，回退到原 Bili2Text 安装的 venv
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    if exist "..\bili2text\.venv\Scripts\python.exe" (
        set "PY=..\bili2text\.venv\Scripts\python.exe"
    ) else (
        echo [ERROR] 未找到 Python 虚拟环境，请先安装依赖。
        pause
        exit /b 1
    )
)

start "Bili2Text Plus" "%PY%" window.py
