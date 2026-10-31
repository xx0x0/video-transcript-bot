#!/bin/bash
# 启动 douyin-bot：加载 .env 后运行 bot.py
# 优先用本目录 .venv（VPS 由 install.sh 创建），否则用 Homebrew 的 python3.11（Mac）
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "❌ 缺少 .env 文件"
    exit 1
fi

set -a
source .env
set +a

if [ -x .venv/bin/python ]; then
    exec .venv/bin/python bot.py
fi
exec /opt/homebrew/bin/python3.11 bot.py
