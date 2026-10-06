@echo off
chcp 65001 >nul
REM ============================================================
REM  rag-kb 一键启动
REM  双击本文件即可，无需记命令
REM ============================================================
title rag-kb 混合检索服务

cd /d D:\workspace\AI\projects\rag-kb

echo.
echo  ============================================
echo   rag-kb 混合检索服务
echo  ============================================
echo.

REM --- 检查数据库是否在跑 ---
netstat -ano | findstr ":5432.*LISTENING" >nul
if errorlevel 1 (
    echo  [数据库] 未运行
    echo.
    echo   本项目的检索索引是本地缓存，**不依赖数据库也能用**：
    echo     OK  网页界面 / 语义问答 / 拒答
    echo     X   结构化查询 / 数据浏览 / 冲突记录
    echo.
    echo   想启用完整功能，请先启动 Docker Desktop，然后运行：
    echo     cd D:\workspace\AI\projects\rag-kb\docker
    echo     docker compose up -d postgres
    echo.
) else (
    echo  [数据库] 已就绪
    echo.
)

REM --- 选端口：默认 8765（避开被代理劫持的 8000/8080）---
set PORT=8765

echo  [检查端口 %PORT%]...
netstat -ano | findstr ":%PORT%.*LISTENING" >nul
if not errorlevel 1 (
    echo.
    echo   端口 %PORT% 已被占用 —— 可能服务已经在跑了。
    echo.
    echo   直接打开浏览器访问：http://127.0.0.1:%PORT%
    echo.
    echo   若要停止它，按 Ctrl+C 然后输入：
    echo     netstat -ano ^| findstr ":%PORT%"    查出 PID
    echo     taskkill /PID ^<PID^> /F
    echo.
    start "" http://127.0.0.1:%PORT%
    pause
    exit /b 0
)

echo  [启动中]...
echo.
echo  ------------------------------------------------------------
echo   网页界面：http://127.0.0.1:%PORT%
echo   接口文档：http://127.0.0.1:%PORT%/docs
echo   ------------------------------------------------------------
echo.
echo   ★ 保持这个窗口开着，关掉窗口服务就停了
echo   ★ 启动耗时约 10-20 秒（首次加载检索索引）
echo.
echo  正在自动打开浏览器...

REM 延迟打开浏览器（等服务起来）
start "" cmd /c "timeout /t 18 /nobreak >nul && start http://127.0.0.1:%PORT%"

echo.
echo  启动命令（供参考）：
echo    python api/main.py --port %PORT% --no-vector
echo.

REM --- 启动服务 ---
python api/main.py --port %PORT% --no-vector

echo.
echo  服务已停止。
pause