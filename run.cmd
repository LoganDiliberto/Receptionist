@echo off
REM Convenience wrapper: launches the receptionist end-to-end (server + ngrok)
REM using the project's virtual environment Python. Any args are passed through.
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" run.py %*
endlocal
