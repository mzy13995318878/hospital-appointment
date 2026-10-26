@echo off
rem ============================================================
rem  一键启动演示服务（http://127.0.0.1:8000）
rem
rem  下面三条每一条都是踩过坑才定下来的，改任何一个都会让演示卡住：
rem
rem  1. 必须用隔离环境里的 uvicorn.exe 绝对路径。
rem     PATH 里那个 python 是裸解释器，没装 fastapi/uvicorn，
rem     直接敲 `uvicorn app.main:app` 只会得到 No module named uvicorn。
rem
rem  2. 必须 --host 0.0.0.0。
rem     默认只监听 127.0.0.1，那样从手机或同局域网的另一台机器访问
rem     会一直转圈连不上，看起来就像"页面卡死"。
rem
rem  3. 必须把输出重定向到文件。
rem     uvicorn 的 stdout 若接到一个没人读的管道（比如父终端被中断），
rem     管道缓冲区一满，写日志的操作就会永久阻塞 ——
rem     整个事件循环跟着停摆，浏览器表现就是"一直转圈、卡住不动"。
rem     重定向到 server.log 后，写日志永远不会卡住。
rem ============================================================

setlocal

rem 依赖装在 WorkBuddy 的隔离环境里，不在系统 python 上
set "UVICORN=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\uvicorn.exe"

if not exist "%UVICORN%" (
    echo [错误] 找不到 uvicorn：%UVICORN%
    echo        依赖没装好，先执行：
    echo        "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m pip install fastapi uvicorn
    exit /b 1
)

rem 切到项目根目录（本文件在 scripts\ 下，往上退一级）
cd /d "%~dp0.."

echo 启动中，访问 http://127.0.0.1:8000 ，日志见 server.log ，Ctrl+C 停止
"%UVICORN%" app.main:app --host 0.0.0.0 --port 8000 --log-level info > server.log 2>&1

endlocal
