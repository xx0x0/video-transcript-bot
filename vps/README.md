# VPS 部署（精简依赖）

同一个 `bot.py`，靠 `.env` 两个开关跑在没有模型、没有浏览器的 VPS 上：

| 开关 | VPS 填 | 关掉后 |
|---|---|---|
| `ENABLE_TRANSCRIBE=0` | 0 | 不跑 Whisper、不调 Ollama；视频只带标题 / 推文原文 |
| `ENABLE_SCREENSHOT=0` | 0 | 不装 Playwright；X 长推/文章直接发文字，非视频的未知链接忽略，腾讯新闻视频不支持，抖音图文没有浏览器兜底 |

保留：抖音视频/图文（无水印、全部原图、标题）、X 视频/配图/推文全文（FxTwitter）、YouTube/B站/TikTok 等 yt-dlp 下载、口令、白名单、大文件 Pyrogram、原片保留。

## 部署（Ubuntu / Debian）

```bash
sudo git clone https://github.com/xx0x0/video-transcript-bot.git /opt/douyin-bot
sudo chown -R $USER /opt/douyin-bot
cd /opt/douyin-bot && sudo bash vps/install.sh   # ffmpeg + venv + 精简依赖 + systemd；自动生成 .env 并关掉两个开关
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
| 更新 yt-dlp | 每次重启自动升级；手动 `.venv/bin/pip install -U yt-dlp` |
| 清理下载 | 原片不自动删，在 `SAVE_DIR`（默认 `/opt/douyin-bot/downloads`）；`find /opt/douyin-bot/downloads -type f -mtime +7 -delete` 可进 crontab |
| 前台调试 | `sudo systemctl stop douyin-bot && ./run.sh`（run.sh 自动用 .venv） |

## 本目录文件

| 文件 | 作用 |
|---|---|
| `install.sh` | 一键安装（在仓库根目录 `sudo bash vps/install.sh`） |
| `douyin-bot.service` | systemd 模板，install.sh 填路径后写入 `/etc/systemd/system/` |
| `requirements.txt` | 精简依赖：telegram、requests、Pillow、yt-dlp、kurigram |
