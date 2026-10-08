# Video Transcript Bot · 抖音/X 视频文案提取 Telegram Bot

A Telegram bot that extracts transcripts from short-form video (Douyin, X/Twitter, YouTube, Bilibili) and summarises them with **local AI** — Whisper for speech-to-text, Ollama for summarisation. Runs entirely on your Mac: free, offline, nothing leaves your machine.

一个运行在本地 Mac 上的 Telegram Bot，支持抖音、X(Twitter)、YouTube、B站等平台的视频文案提取与 AI 总结，完全免费，数据不上云。

---

## 🚀 快速启动

```bash
git clone https://github.com/xx0x0/video-transcript-bot.git
cd video-transcript-bot
cp .env.example .env       # 编辑 .env 填 BOT_TOKEN / ALLOWED_USER / ALLOWED_GROUP
chmod +x run.sh
./run.sh
```

> **首次运行前**还需要按下方完整教程安装 Python 依赖、playwright、ffmpeg、Whisper、Ollama 等环境。详情见下方「🚀 安装步骤」一节。

---

## ✨ 功能

- 📥 **视频获取** — 抖音无需登录，其他平台需配置 cookie
- 🎙️ **本地语音转文案** — 使用 Whisper，完全离线，中文识别准确
- 🤖 **AI 文案梳理** — 使用本地 Ollama + qwen2.5:7b，超过800字自动梳理
- 🔗 **链接去追踪** — 抖音短链自动解析成干净的数字 ID 链接
- 📝 **X 推文提取** — 自动提取推文原文，不转文案
- 🖼️ **图文识别** — 自动识别抖音图文笔记并提示
- 🔒 **白名单保护** — 只响应指定用户和群组

---

## 📤 输出格式

**抖音/其他平台：**
```
[视频文件]

视频标题：xxx

文案：
[Whisper 识别的完整文案]

原视频链接：https://www.douyin.com/video/xxx
```

**文案超过800字时额外输出：**
```
视频标题：xxx

梳理后的文案：
[AI 梳理内容]

原视频链接：https://...
```

**X/Twitter：**
```
[视频文件]

📝 推文内容：
[推文原文]

🔗：https://x.com/...
```

---

## 📋 系统要求

- macOS（Apple Silicon 推荐，M1/M2/M3/M4）
- Python 3.11+
- 16GB 内存以上

---

## 🚀 安装步骤

### 1. 安装基础工具

```bash
brew install node@24 yt-dlp ffmpeg python@3.11 uv ollama
echo 'export PATH="/opt/homebrew/opt/node@24/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### 2. 安装 Python 依赖

```bash
/opt/homebrew/bin/pip3.11 install openai-whisper python-telegram-bot ffmpeg-python dashscope mcp tqdm
```

### 3. 安装 douyin-mcp-server

```bash
git clone https://github.com/yzfly/douyin-mcp-server.git ~/douyin-mcp-server
cd ~/douyin-mcp-server
uv sync --python 3.11
```

### 4. 安装本地 AI 模型

```bash
brew services start ollama
ollama pull qwen2.5:7b
```

### 5. 配置 Telegram Bot

1. 找 **@BotFather** 创建 bot，保存 Token
2. `/setprivacy` → 选 bot → `Disable` — 禁用隐私模式，让 bot 可以读取群里所有消息
3. 确认群组功能已开启（Groups enabled），这样才能把 bot 加入群组

> **说明：** Privacy mode 设为 Disabled 后，bot 拥有全量读取权限，可以抓取聊天窗口中所有文本，不需要被 @ 才能响应。

### 6. 配置 Cookie（下载其他平台视频需要）

**X/Twitter：**
```bash
printf '# Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\t你的auth_token\n.x.com\tTRUE\t/\tTRUE\t2147483647\tct0\t你的ct0\n' > ~/x-cookies.txt
```

> 在浏览器 F12 → Application → Cookies → x.com 里找 auth_token 和 ct0

### 7. 配置并启动

复制配置模板并填入你自己的值：

```bash
cp .env.example .env
# 用编辑器打开 .env，填入：
#   BOT_TOKEN        — 从 @BotFather 拿到的 token
#   ALLOWED_USER     — 允许私聊的用户 ID（多个用英文逗号分隔）
#   ALLOWED_GROUP    — 允许响应的群 ID（多个用英文逗号分隔）
#   BADNEWS_COOKIES  — 可选，巴比馒头官网 Cookie
#   TELEGRAM_API_ID  — 可选，my.telegram.org 申请；填了才支持发 >50MB 大文件（最高 2GB）
#   TELEGRAM_API_HASH— 可选，同上，与 API_ID 成对填写
```

启动：

```bash
chmod +x run.sh    # 首次运行赋予可执行权限
./run.sh           # 自动加载 .env 后启动 bot.py
```

> **`run.sh` 做了什么：** 检查 `.env` 是否存在 → `source .env` 注入环境变量 → 用 `python3.11` 跑 `bot.py`。
>
> **如果运行失败**，检查 `bot.py` 里的路径（`SAVE_DIR`、`DOUYIN_MCP`）是否与实际文件位置一致，以及 `.env` 是否填全。

---

### 🌏 国内用户额外说明

Telegram 在中国大陆被封锁，Bot 需要连接 `api.telegram.org`，**必须配置代理才能正常运行**。

如果使用虚拟环境安装的依赖，启动方式改为：

```bash
source ~/douyin-mcp-server/venv/bin/activate
set -a && source .env && set +a    # 加载 .env 到环境变量
python3 ~/douyin-bot/bot.py
```

---

## 📱 支持平台

| 平台 | 视频 | 文案 | 需要 Cookie |
|------|------|------|------------|
| 抖音 | ✅ | ✅ | ❌ 无需登录 |
| TikTok | ✅ | ✅ | ❌ |
| X/Twitter | ✅ | ✅ 推文原文 | ✅ 需要 |
| YouTube | ✅ | ✅ | ❌ |
| B站 | ✅ | ✅ | ❌ |
| Instagram | ✅ | ✅ | ✅ 需要 |
| 微博 | ✅ | ✅ | ❌ |
| 快手 | ✅ | ✅ | ❌ |
| 小红书图文 | ❌ | ❌ | ✅ 需要 |

---

## ⚠️ 注意事项

- 视频不自动删除，需手动清理 `~/Downloads/抖音/`
- 文案超过800字自动触发 AI 梳理
- 抖音无需登录可以直接下载视频、提取文案
- Instagram、小红书、X 等平台需要配置 cookie 才能下载
- 视频超过 50MB：配了 `TELEGRAM_API_ID`/`HASH` 则走 Pyrogram 直发原画（上限 2GB），超 2GB 提示本地路径手动提取；未配则自动压缩发送（上限 200MB），超 200MB 提示手动提取
- 图文内容需要 cookie 才能自动提取，未配置时请手动保存图片
- Whisper 首次运行会下载模型约 150MB
- 10分钟视频处理约需 15-20 分钟，请耐心等待
- Bot 需保持 Mac 开机且终端运行
- Telegram 保留24小时内未读消息，重启后会自动补处理

---

## ⚙️ 开机自启

```bash
cat > ~/Library/LaunchAgents/com.douyin.bot.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.douyin.bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/python3.11</string>
        <string>/Users/你的用户名/douyin-bot/bot.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/douyin-bot.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/douyin-bot-error.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.douyin.bot.plist
```

> 记得把 `/Users/你的用户名/douyin-bot/bot.py` 改成你实际的文件路径。

---

## 📜 更新日志

### 2026-10-08
- 🆕 **大文件改走 Pyrogram/MTProto 发送（上限 50MB→2GB）** — Bot API 上传硬限 50MB 导致长视频被压成 480×270 极糊；新增 Pyrogram（同一 bot token，MTProto 直连），>50MB 视频直发原画不压缩，X 选档上限放宽到 1900MB 可取 720P 原画；需在 `.env` 填 `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` 启用，留空则维持原 50MB 行为
- ⚠️ **超过 2GB 的视频 TG 仍传不了** — 提示本地路径手动提取

### 2026-09-01
- 🆕 **X 视频下载改走 FxTwitter 直链** — yt-dlp 的 X 提取器被 X API 改版打挂（最新版也报 Could not authenticate you），改用 FxTwitter 给的 video.twimg.com mp4 直链（免鉴权免 cookie），多档码率自动选不超 Telegram 50MB 上限的最高画质，yt-dlp 仅作兜底
- 🔧 **X 视频一律不转文案（定稿）** — X 视频多为无意义内容，默认模式不再试探/转录，只发 视频+推文原文+链接；15 秒语音试探机制废弃（连贯性判定拦不住成人视频）；抖音等其他平台恢复直接全程转录
- 🔧 **恢复：视频发送后保留原片** — 修复 8/18 重构误改的"发完即删"，原视频重新保留在 `~/Downloads/抖音/`，只清理转码副本和失败残留
- 🔧 **yt-dlp 升级 2026.03.17 → 2026.08.19**

### 2026-08-31
- 🆕 **X 文章/长推截图改本地渲染卡片** — 不再用浏览器打开 x.com（headless 被 403、有头逐屏截图对不齐），FxTwitter 数据渲染成本地 HTML 推文卡片后整页截图+精确切分，零重复零遮挡，无需 cookie
- 🆕 **X 卡片截图接 AI 短梳理** — 本地 Ollama qwen2.5:7b 出 3-5 句要点，放 caption 或相册后单发
- 🔧 **X 内容解析改走 FxTwitter API** — X 删光页面 data-testid 且对 headless 一律 403，分类/正文/引用/配图改由 `api.fxtwitter.com` 提供（长推全文、X 文章正文、引用推全文）

### 2026-08-18
- 🔧 **`_process()` 拆分为平台专属 handler** — 消除 500 行巨函数，抖音/bad.news/腾讯新闻/通用 yt-dlp 各自独立

### 2026-05-14 ~ 05-19
- 🆕 **X 长推文全文提取（05-19）** — X Premium 长推（NoteTweet）通过 syndication API 探测，命中后用 Playwright 抓完整正文，覆盖 yt-dlp 截断的 description
- 🆕 **纯视频平台失败时静默（05-18）** — YouTube/Bilibili/Instagram/快手/小红书 下载失败不再响应错误，直接静默跳过
- 🆕 **腾讯新闻视频下载（05-17）** — `news.qq.com` / `view.inews.qq.com` 用 Playwright 拦截 m3u8 + ffmpeg 转封装，失败回退截图
- 🆕 **未知链接智能分流（05-17）** — 未知链接先用 yt-dlp 探测视频，有视频走下载，没视频截图；视频下载失败自动回退截图
- 🆕 **口令控制** — 消息里加 `/skip`/`跳过` 忽略、`/title`/`标题` 只发视频不转文案、`/text`/`文案` 只提取文案不发视频
- 🆕 **启动通知** — bot 启动时自动给 BOT_OWNER 发消息，列出支持口令；新增 `BOT_OWNER` 环境变量
- 🔧 **whisper 试探扩展到所有平台** — 之前只有 X 才截前10秒试探，现在所有平台统一截前15秒，不连贯直接跳过全程转录
- 🔧 **幻觉处理升级** — `clean_hallucination` 新增全局占比检测，幻觉行超过50%直接返回空，解决从头就乱的情况；新增常见幻觉短语黑名单
- 🔧 **文章链接逻辑收紧** — 改为白名单逻辑，只有明确列出的平台才走截图，其余链接一律忽略
- 🆕 **github.com 加入平台列表** — 发 github 链接自动截图发回

### 2026-04-29
- 🆕 **README 顶部新增「快速启动」5 行命令** — clone → 复制 .env.example → 改密 → `./run.sh`，对照即可跑
- 🆕 **新增 `.env.example` 模板** — 配置项 + 注释一目了然，含获取 user ID / 群 ID 的方法
- 🆕 **新增 `run.sh` 启动脚本** — 自动加载 `.env` 后启动 bot，免手动 `export`
- 🔧 **白名单支持多用户 + 多群组** — `ALLOWED_USER` / `ALLOWED_GROUP` 改为支持英文逗号分隔多个 ID，bot.py 内部用 set 判断
- 🔧 **`.gitignore` 追加 `*.log`** — 避免 `bot.log` 等运行时日志意外入库
- 📝 **README 启动章节重写** — 从「编辑 bot.py 填值 + python3 bot.py」改为「复制 .env.example → 填值 + ./run.sh」，与代码实际行为一致

### 2026-04-22
- 🔧 **抖音提取自动重试** — 网络抖动不再直接失败，自动重试 3 次（递增等待 2s→4s），长时间挂机更稳定
- 🆕 **超 50MB 视频自动压缩** — 50~200MB 视频用 ffmpeg 自动压缩到 50MB 以内再发送，超过 200MB 才提示手动提取
- 🔧 **抖音 MCP 请求加 timeout** — 防止网络挂起时无限阻塞，提取页面 15s / 下载视频 60s 超时

### 2026-04-16
- 🆕 **任意文章链接自动截图** — 非视频平台链接自动走 playwright 本地截图，图片+标题+原链接一起发出
- 🆕 **X 文章自动提取** — 隐藏侧栏/登录弹窗，分段滚动截图，内容清晰不压糊
- 🆕 **X/微博视频下载失败自动回退** — 纯文字/图片推文也能通过截图方式发送
- 🔧 **截图体验优化** — 滚动分段截图 + 高 DPR（1200×1600 @2x），发 Telegram 相册更清晰
- 🔧 **图片尺寸规范化（Pillow）** — 单边 >8000px 自动缩放、宽高比 >18:1 自动切分，修复 `Photo_invalid_dimensions`
- 🐛 **修复 whisper 幻觉** — 双层防护：
  - 参数层：`--condition_on_previous_text False`、提高 `--no_speech_threshold` 等
  - 后处理层：清理尾部俄/韩/日/阿/泰/希腊字符混杂的乱码行

### 2026-04-08
- 🆕 **抖音图文笔记提取** — 无需登录直接提取图文笔记的多张图片，作为相册发送
- 🆕 **bad.news 成人内容支持** — 通过 cookies 抓取视频，跳过 whisper 转录直发
- 🔧 **文案发送策略优化** — 视频 caption 随视频发送，超长文案分条发送
- 🐛 修复：caption 截断时保留末尾原链接
- 🐛 修复：whisper 显式指定 turbo 模型，避免回落到默认 small

### 2026-04-03
- 🎉 **项目首次发布**
- 抖音、X/Twitter、YouTube、B站 等多平台视频下载与文案提取
- 集成 Whisper 本地语音转文字
- 集成 Ollama + qwen2.5:7b 本地 AI 文案梳理
- Telegram Bot 白名单保护

---

## ⚠️ 免责声明

本项目仅供学习和个人使用，请遵守相关平台服务条款及当地法律法规。
