# douyin-bot 精简版（VPS）

只做三件事：**下载视频、下载图片、提取现成文字**（标题 / 简介 / 推文原文）。
不含语音转文案（Whisper）、AI 梳理（Ollama）、网页截图（Playwright），所以没有浏览器和模型依赖，1 核 1G 的 VPS 就能跑。

## 功能对照

| 平台 | 视频 | 图片 | 文字 | 备注 |
|---|---|---|---|---|
| 抖音 视频 | ✅ 无水印 | — | 标题 | 抓 iesdouyin 分享页内嵌数据，无需登录（已实测） |
| 抖音 图文 | — | ✅ 全部原图 | 标题 | 同上路径读 `item_list[0].images`（代码沿用完整版字段，未拿到图文链接实测） |
| X / Twitter | ✅ 720P 直链 | ✅ 配图 | 推文全文 / 文章正文 / 引用 | FxTwitter API，无需 cookie |
| YouTube / B站 / TikTok / 快手 / Instagram / 微博 | ✅ yt-dlp | — | 标题 + 简介 | 需登录的平台失败静默跳过 |

口令（放在消息任意位置）：`标题`/`/title` 只发视频+标题；`文案`/`/text` 只发文字不发视频；`跳过`/`/skip` 忽略。

大文件：≤50MB 走 Bot API；50~200MB 自动压缩；配了 `TELEGRAM_API_ID/HASH` 则 >50MB 走 MTProto 直发原画（上限 2GB）。

## 部署（Ubuntu / Debian）

```bash
# 1. 把 vps/ 目录放到服务器（任选一种）
scp -r vps/ user@你的VPS:/opt/douyin-bot
# 或在服务器上：git clone https://github.com/xx0x0/video-transcript-bot.git && sudo mv video-transcript-bot/vps /opt/douyin-bot

# 2. 一键安装（装 ffmpeg、建 venv、装依赖、注册 systemd）
cd /opt/douyin-bot && sudo bash install.sh

# 3. 填配置
nano .env        # BOT_TOKEN、ALLOWED_USER 必填；其余可选

# 4. 启动 & 看日志
sudo systemctl start douyin-bot
journalctl -u douyin-bot -f
```

启动成功后 bot 会给 `BOT_OWNER`（默认 ALLOWED_USER 第一个）发一条「已启动」消息。

## 日常运维

| 操作 | 命令 |
|---|---|
| 重启 | `sudo systemctl restart douyin-bot` |
| 停止 | `sudo systemctl stop douyin-bot` |
| 日志 | `journalctl -u douyin-bot -f` |
| 更新代码 | 覆盖 `/opt/douyin-bot/*.py` 后 `restart` |
| 更新 yt-dlp | 每次重启自动升级；手动：`.venv/bin/pip install -U yt-dlp` |
| 清理下载 | 原片发完**不自动删**，在 `SAVE_DIR`；清 7 天前的：`find /opt/douyin-bot/downloads -type f -mtime +7 -delete`（可加进 crontab） |
| 手动调试 | `sudo systemctl stop douyin-bot && ./run.sh` |

## 与完整版的差异

- 去掉：Whisper 转录、Ollama 梳理、Playwright 截图（文章 / 腾讯新闻 / bad.news / X 长推卡片）、pkb 归档
- X 长推 / 文章不再渲染成图片卡片，改为直接发文字（超 4000 字自动分条）
- 抖音不再用移动端 feed API（2026-10-31 实测它已不认 `aweme_id`，返回随机推荐流，会下到无关视频），改为 requests 带 session 抓 iesdouyin 分享页：首次响应种 `ttwid` cookie，第二次起 `_ROUTER_DATA.loaderData.*.videoInfoRes.item_list[0]` 里有直链 / 图文 / 标题；`/playwm/` 换 `/play/` 即无水印
- 内容被过滤（地区限制 / 已删除）时分享页返回 `filter_list`，bot 直接提示不可用，不再重试
- 非白名单平台链接一律忽略（完整版会截图）
- 路径全部可配：`SAVE_DIR`、`X_COOKIES`、`DOUYIN_COOKIES`

## 文件说明

| 文件 | 作用 |
|---|---|
| `bot_lite.py` | 主程序：Telegram 收发、平台分流、视频发送 |
| `douyin.py` | 抖音解析（从 douyin-mcp-server 抽出的纯函数，无 mcp 依赖） |
| `fx_twitter.py` | X 解析（与完整版同一文件） |
| `requirements.txt` | Python 依赖 |
| `install.sh` | 一键安装脚本 |
| `douyin-bot.service` | systemd 模板（install.sh 填路径后写入 `/etc/systemd/system/`） |
| `run.sh` | 手动前台启动（调试） |
| `.env.example` | 配置模板 |
