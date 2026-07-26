@echo off
cd /d "%~dp0"
set "PYTHON_EXE="
call :try_python "%~dp0.venv\Scripts\python.exe"
if defined PYTHON_EXE goto run
call :try_python "C:\Users\oracl\AppData\Local\Programs\Python\Python312\python.exe"
if defined PYTHON_EXE goto run
call :try_python "python"
if defined PYTHON_EXE goto run

echo No compatible Python runtime found for the bot.
echo Expected a Python with fastapi and nonebot installed.
exit /b 1

:try_python
set "CANDIDATE=%~1"
if /i not "%CANDIDATE%"=="python" if not exist "%CANDIDATE%" goto :eof
"%CANDIDATE%" -c "import fastapi, nonebot" >nul 2>nul
if errorlevel 1 goto :eof
set "PYTHON_EXE=%CANDIDATE%"
goto :eof

:run
"%PYTHON_EXE%" scripts\run_private_qq_bot.py
exit /b %ERRORLEVEL%
