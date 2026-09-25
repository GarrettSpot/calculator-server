@echo off
python "%~dp0bcurl.py" %*
exit /b %ERRORLEVEL%
