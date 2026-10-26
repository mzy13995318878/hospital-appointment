#!/usr/bin/env bash
# 一键启动演示服务（http://127.0.0.1:8000）
#
# 三条必须这么写的规矩，每条都对应一次真实的"演示卡死"事故：
#
# 1. 用隔离环境里的 uvicorn 绝对路径。
#    PATH 里的 python 是裸解释器，没装 fastapi/uvicorn，
#    直接敲 `uvicorn app.main:app` 只会得到 No module named uvicorn。
#
# 2. --host 0.0.0.0。
#    默认只监听 127.0.0.1，从手机或局域网另一台机器访问会一直转圈。
#
# 3. 输出重定向到文件。
#    uvicorn 的 stdout 若接到没人读的管道（父终端被中断就会这样），
#    缓冲区一满，写日志永久阻塞，事件循环停摆 ——
#    浏览器表现就是"一直转圈、卡住不动"。
#
# 用法：
#   bash scripts/serve.sh              # 前台跑，Ctrl+C 停
#   nohup bash scripts/serve.sh &      # 后台跑，关掉终端也不停

set -u

UVICORN="$HOME/.workbuddy/binaries/python/envs/default/Scripts/uvicorn.exe"

if [ ! -f "$UVICORN" ]; then
    echo "[错误] 找不到 uvicorn：$UVICORN"
    echo "        先装依赖：\"$HOME/.workbuddy/binaries/python/envs/default/Scripts/python.exe\" -m pip install fastapi uvicorn"
    exit 1
 fi

# 切到项目根目录：本文件位于 scripts/ 下
cd "$(dirname "$0")/.."

echo "启动中 → http://127.0.0.1:8000 （日志 server.log）"
exec "$UVICORN" app.main:app --host 0.0.0.0 --port 8000 --log-level info > server.log 2>&1
