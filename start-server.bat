@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 RAG 服务...
echo 启动后浏览器打开 http://127.0.0.1:8900/
echo Key 写在 config.json，别提交到仓库。关掉窗口即停止。
echo.
python rag_server.py
pause
