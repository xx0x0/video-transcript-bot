#!/bin/bash
# Ubuntu / Debian VPS 一键安装：系统依赖 + venv + pip 依赖 + systemd 服务
# 用法：sudo bash install.sh   （在本目录执行；目录建议放 /opt/douyin-bot）
set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-$(whoami)}"

echo "▶ 安装系统依赖（python3-venv ffmpeg）"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg

echo "▶ 创建 venv 并安装 Python 依赖"
sudo -u "$RUN_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
    sudo -u "$RUN_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo "⚠️ 已生成 $APP_DIR/.env，请先填 BOT_TOKEN / ALLOWED_USER 再启动"
fi
chmod +x "$APP_DIR/run.sh"

echo "▶ 写入 systemd 服务"
sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__RUN_USER__#$RUN_USER#g" \
    "$APP_DIR/douyin-bot.service" > /etc/systemd/system/douyin-bot.service
systemctl daemon-reload
systemctl enable douyin-bot

cat << MSG

✅ 安装完成。下一步：
   1. nano $APP_DIR/.env          # 填 BOT_TOKEN、ALLOWED_USER 等
   2. sudo systemctl start douyin-bot
   3. journalctl -u douyin-bot -f  # 看日志
MSG
