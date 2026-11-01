#!/bin/bash
# Ubuntu / Debian VPS 一键安装：系统依赖 + venv + pip 依赖 + systemd 服务
# 用法：在仓库根目录执行  sudo bash vps/install.sh   （仓库建议放 /opt/douyin-bot）
# 装的是"精简依赖"（vps/requirements.txt）：无 Whisper / Ollama，对应 .env 里 ENABLE_TRANSCRIBE=0；
# 装 Playwright + Chromium（浏览器放仓库内 .pw-browsers/），ENABLE_SCREENSHOT=1 网页截图全开
set -e
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(whoami)}"

echo "▶ 安装系统依赖（python3-venv ffmpeg 中文字体）"
apt-get update -qq
# fonts-noto-cjk：截图中文页面不装会全是方块
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg fonts-noto-cjk

echo "▶ 创建 venv 并安装 Python 依赖"
sudo -u "$RUN_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/vps/requirements.txt"

echo "▶ 安装 Chromium（Playwright 截图用，放 $APP_DIR/.pw-browsers）"
# 浏览器固定放仓库内，不依赖运行用户的 ~/.cache；systemd 服务里同样设了 PLAYWRIGHT_BROWSERS_PATH
"$APP_DIR/.venv/bin/playwright" install-deps chromium
sudo -u "$RUN_USER" env PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/.pw-browsers" \
    "$APP_DIR/.venv/bin/playwright" install chromium

if [ ! -f "$APP_DIR/.env" ]; then
    sudo -u "$RUN_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    # VPS 默认关掉转文案（无模型），截图开着，路径指到仓库内
    sudo -u "$RUN_USER" sed -i \
        -e 's#^ENABLE_TRANSCRIBE=.*#ENABLE_TRANSCRIBE="0"#' \
        -e 's#^ENABLE_SCREENSHOT=.*#ENABLE_SCREENSHOT="1"#' \
        -e "s#^SAVE_DIR=.*#SAVE_DIR=\"$APP_DIR/downloads\"#" \
        -e "s#^X_COOKIES=.*#X_COOKIES=\"$APP_DIR/x-cookies.txt\"#" \
        -e "s#^DOUYIN_COOKIES=.*#DOUYIN_COOKIES=\"$APP_DIR/douyin-cookies.txt\"#" \
        -e 's#^ALLOWED_GROUP=.*#ALLOWED_GROUP=""#' \
        "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo "⚠️ 已生成 $APP_DIR/.env（转文案关、截图开），请先填 BOT_TOKEN / ALLOWED_USER 再启动"
fi
chmod +x "$APP_DIR/run.sh"

echo "▶ 写入 systemd 服务"
sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__RUN_USER__#$RUN_USER#g" \
    "$APP_DIR/vps/douyin-bot.service" > /etc/systemd/system/douyin-bot.service
systemctl daemon-reload
systemctl enable douyin-bot

cat << MSG

✅ 安装完成。下一步：
   1. nano $APP_DIR/.env          # 填 BOT_TOKEN、ALLOWED_USER
   2. sudo systemctl start douyin-bot
   3. journalctl -u douyin-bot -f  # 看日志
MSG
