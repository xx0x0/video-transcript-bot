#!/bin/bash
# 手动启动（调试用）：加载 .env 后运行 bot_lite.py。生产用 systemd（见 README）。
set -e
cd "$(dirname "$0")"
[ -f .env ] || { echo "❌ 缺少 .env，先 cp .env.example .env 并填好"; exit 1; }
set -a; source .env; set +a
exec ./.venv/bin/python bot_lite.py
