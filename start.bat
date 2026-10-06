@echo off
REM ============================================================
REM   rag-kb quick start
REM   Double-click to run. Keep this window OPEN.
REM ============================================================

cd /d "D:\workspace\AI\projects\rag-kb"

echo.
echo  ============================================
echo   rag-kb Search Service
echo  ============================================
echo.

REM ------------------------------------------------------------
REM  Find a Python that actually has the dependencies.
REM  Plain "python" may point to a bundled interpreter without
REM  fastapi, so probe candidates with a real import test.
REM ------------------------------------------------------------
set PY=

if exist "C:\Users\28188\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
    "C:\Users\28188\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -c "import fastapi, uvicorn, psycopg2" >nul 2>&1
    if not errorlevel 1 set PY=C:\Users\28188\.workbuddy\binaries\python\envs\default\Scripts\python.exe
)

if not defined PY if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe -c "import fastapi, uvicorn, psycopg2" >nul 2>&1
    if not errorlevel 1 set PY=%CD%\.venv\Scripts\python.exe
)

if not defined PY (
    for %%P in (python.exe py.exe) do (
        if not defined PY (
            %%P -c "import fastapi, uvicorn, psycopg2" >nul 2>&1
            if not errorlevel 1 set PY=%%P
        )
    )
)

if not defined PY (
    echo  [ERROR] No Python with the required packages was found.
    echo.
    echo  Required: fastapi, uvicorn, psycopg2-binary, torch, transformers
    echo.
    echo  Fix with:  pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo  [OK] Python found
echo.
echo  Starting service on port 8765 ...
echo  Browser will open automatically in ~18 seconds.
echo.
echo  ------------------------------------------------------------
echo   Web UI:http://127.0.0.1:8765
echo   API docs:   http://127.0.0.1:8765/docs
echo  ------------------------------------------------------------
echo.
echo   KEEP THIS WINDOW OPEN.
echo   Closing it stops the service.
echo.

REM start.py handles: DB status, port check, delayed browser launch
"%PY%" start.py --port 8765
set RC=%ERRORLEVEL%

echo.
echo  ============================================
if not "%RC%"=="0" (
    echo   Exited with code %RC%
    echo  ============================================
    echo.
    echo   The lines above show why.
    echo   Common fixes:
    echo     - another port:  python start.py --port 8766
    echo     - missing deps:  pip install -r requirements.txt
) else (
    echo   Service stopped
    echo  ============================================
)
echo.
echo   Press any key to close this window.
pause >nul