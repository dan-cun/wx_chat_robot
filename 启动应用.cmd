@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
if /i "%~1"=="--check" (
    "%~dp0本地应用\wechatbot-new\.venv\Scripts\python.exe" -X utf8 "%~dp0本地应用\wechatbot-new\应用管理.py" --help
    exit /b
)
"%~dp0本地应用\wechatbot-new\.venv\Scripts\python.exe" -X utf8 "%~dp0本地应用\wechatbot-new\应用管理.py" start --open-browser
set "LaunchExitCode=%errorlevel%"
if not "%LaunchExitCode%"=="0" pause
exit /b %LaunchExitCode%
