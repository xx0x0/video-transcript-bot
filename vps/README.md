# VPS 部署（精简依赖）

同一个 `bot.py`，靠 `.env` 两个开关跑在没有模型的 VPS 上：

| 开关 | VPS 填 | 说明 |
|---|---|---|
| `ENABLE_TRANSCRIBE=0` | 0 | 不跑 Whisper、不调 Ollama；视频只带标题 / 推文原文 |
| `ENABLE_SCREENSHOT=1` | 1 | install.sh 装 Playwright + Chromium（放仓库内 `.pw-browsers/`）+ 中文字体；文章/长推截图、腾讯新闻视频、抖音图文兜底都可用。填 0 可关（X 长推/文章只发文字，非视频未知链接忽略） |

保留：抖音视频/图文（无水印、全部原图、标题）、X 视频/配图/推文全文（FxTwitter）、YouTube/B站/TikTok 等 yt-dlp 下载、口令、白名单、大文件 Pyrogram、原片保留。

VPS 没有显示器：X 的有头浏览器兜底自动退回 headless（`DISPLAY` 为空时）。X 正常路径是 FxTwitter + 本地渲染卡片，不受影响。

**微信视频号**（`weixin.qq.com/sph/...` / `channels.weixin.qq.com`）：微信外拿不到视频地址，走第三方上游 anyfetcher.com 解析（`weixin_channels.py`，不需要浏览器）。上游匿名额度很低（实测一个 IP 一天 2 次左右），用完或上游失效时 bot 回「视频号上游失效：<原因>」。

## 部署（Ubuntu / Debian）

```bash
sudo git clone https://github.com/xx0x0/video-transcript-bot.git /opt/douyin-bot
sudo chown -R $USER /opt/douyin-bot
cd /opt/douyin-bot && sudo bash vps/install.sh   # ffmpeg + 中文字体 + venv + 精简依赖 + Chromium + systemd；自动生成 .env（转文案关、截图开）
nano .env                                       # 填 BOT_TOKEN、ALLOWED_USER
sudo systemctl start douyin-bot
journalctl -u douyin-bot -f
```

启动成功后 bot 给 `BOT_OWNER`（默认 ALLOWED_USER 第一个）发「已启动」消息，首行显示开关状态。

## 日常运维

| 操作 | 命令 |
|---|---|
| 重启 / 停止 | `sudo systemctl restart douyin-bot` / `stop` |
| 日志 | `journalctl -u douyin-bot -f` |
| 更新代码 | `cd /opt/douyin-bot && git pull && sudo systemctl restart douyin-bot` |
| 已部署机器升级到截图版 | `git pull` 后 `sudo bash vps/install.sh`（会补装 Playwright/Chromium/字体并重写 service），再 `nano .env` 把 `ENABLE_SCREENSHOT` 改成 `"1"`，`sudo systemctl daemon-reload && sudo systemctl restart douyin-bot` |
| 更新 yt-dlp | 每次重启自动升级；手动 `.venv/bin/pip install -U yt-dlp` |
| 清理下载 | 原片不自动删，在 `SAVE_DIR`（默认 `/opt/douyin-bot/downloads`）；`find /opt/douyin-bot/downloads -type f -mtime +7 -delete` 可进 crontab |
| 前台调试 | `sudo systemctl stop douyin-bot && ./run.sh`（run.sh 自动用 .venv） |

## 本目录文件

| 文件 | 作用 |
|---|---|
| `install.sh` | 一键安装（在仓库根目录 `sudo bash vps/install.sh`） |
| `douyin-bot.service` | systemd 模板，install.sh 填路径后写入 `/etc/systemd/system/` |
| `requirements.txt` | 精简依赖：telegram、requests、Pillow、yt-dlp、kurigram、playwright |
