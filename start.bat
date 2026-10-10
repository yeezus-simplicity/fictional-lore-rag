@echo off
REM ============================================================
REM   rag-kb quick start
REM   Double-click to run. Keep this window OPEN.
REM ============================================================

REM ★ 用脚本自身所在目录，不要写死绝对路径
REM   （原来写死了开发机的路径，别人 clone 后必然跑不起来）
cd /d "%~dp0"

echo.
echo  ============================================
echo   fictional-lore-rag  Search Service
echo  ============================================
echo.

REM ------------------------------------------------------------
REM  Find a Python that actually has the dependencies.
REM  Plain "python" may point to a bundled interpreter without
REM  fastapi, so probe candidates with a real import test.
REM ------------------------------------------------------------
set PY=

REM ★ 项目内虚拟环境优先（推荐：python -m venv .venv）
if not defined PY if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe -c "import fastapi, uvicorn, psycopg2" >nul 2>&1
    if not errorlevel 1 set PY=%CD%\.venv\Scripts\python.exe
)

REM ★ 其次用 PATH 里的 python / py
REM   （原来这里还探测一条开发机专属的绝对路径，已移除 —— 那对别人无意义）
if not defined PY (
    for %%P in (python.exe py.exe) do (
        if not defined PY (
            %%P -c "import fastapi, uvicorn, psycopg2" >nul 2>&1
            if not errorlevel 1 set PY=%%P
        )
    )
)

REM ------------------------------------------------------------
REM  NOTE: This file is intentionally **English-only**.
REM  A .bat is parsed by cmd.exe byte-by-byte; non-ASCII text can be
REM  mangled by the console code page and — worse — a stray character
REM  can be parsed as a *command*, breaking the script.
REM  Measured: Chinese text here produced mojibake plus
REM  "'xxx' is not recognized as an internal or external command".
REM  => Keep .bat ASCII. Put Chinese docs in README instead.
REM ------------------------------------------------------------

if not defined PY (
    echo  [ERROR] No Python with the required packages was found.
    echo.
    echo  Candidates actually probed, and why each failed:
    echo.
    for %%P in (".venv\Scripts\python.exe" python.exe py.exe) do (
        echo    %%~P
        %%~P -c "import sys;print('        interpreter:',sys.executable)" 2>nul
        %%~P -c "import fastapi" >nul 2>&1
        if errorlevel 1 (echo         MISSING fastapi  ^<- reason) else (echo         fastapi OK)
    )
    echo.
    echo  ------------------------------------------------------------
    echo  Pick one fix:
    echo.
    echo  [A] Run without the vector model  (recommended, much smaller)
    echo      python -m pip install fastapi uvicorn psycopg2-binary
    echo      python start.py --no-vector
    echo.
    echo  [B] Install everything  (includes the ~7GB embedding model)
    echo      python -m pip install -i https://pypi.org/simple -r requirements.txt
    echo.
    echo  [C] Already have a Python that has the deps? Run it directly:
    echo      "path\to\python.exe" start.py --no-vector
    echo.
    echo  TIP: install with "python -m pip", NOT bare "pip" - otherwise
    echo  the packages land in a different interpreter and this script
    echo  still cannot find them.
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