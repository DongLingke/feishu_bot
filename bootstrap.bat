@echo off

where python >nul 2>nul
IF ERRORLEVEL 1 (
    echo python is required.
    EXIT /b 1
)

python -m pip install -r requirements.txt
IF ERRORLEVEL 1 (
    EXIT /b 1
)

python main.py
