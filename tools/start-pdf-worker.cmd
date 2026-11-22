@echo off
setlocal
cd /d "%~dp0"
set "worker_ip=%~1"
if not defined worker_ip (
  echo Usage: start-worker.cmd IP_OF_SECOND_LAPTOP
  pause
  exit /b 1
)
if not exist "worker-token.txt" (
  echo Missing worker-token.txt. Extract the complete archive first.
  pause
  exit /b 1
)
set /p NEXUS_DUAL_WORKER_TOKEN=<worker-token.txt
runtime\python.exe -B -X utf8 -m article_analyzer.dual_worker --bind "%worker_ip%" --port 8091 --qwen "http://%worker_ip%:8080/v1"
if errorlevel 1 pause
