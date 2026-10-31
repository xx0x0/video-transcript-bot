#!/usr/bin/env python3
"""douyin-bot 精简版（VPS 部署）。

只保留：下载视频 / 下载图片 / 提取现成文字（标题、简介、推文原文）。
去掉：Whisper 转录、Ollama 梳理、Playwright 截图、pkb 归档。
无浏览器依赖，系统只需 python3 + ffmpeg（+ yt-dlp 走 pip）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from io import BytesIO
from typing import Sequence

import requests
from PIL import Image
from telegram import InputMediaPhoto, Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

import douyin
import fx_twitter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# ---------------- 配置（由 run.sh / systemd 注入 .env） ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
SAVE_DIR = os.path.expanduser(os.environ.get("SAVE_DIR", "~/douyin-downloads"))
os.makedirs(SAVE_DIR, exist_ok=True)

ALLOWED_USERS = {int(x) for x in os.environ.get("ALLOWED_USER", "").split(",") if x.strip()}
ALLOWED_GROUPS = {int(x) for x in os.environ.get("ALLOWED_GROUP", "").split(",") if x.strip()}
BOT_OWNER = int(os.environ.get("BOT_OWNER", "0") or "0") or (min(ALLOWED_USERS) if ALLOWED_USERS else 0)
if not ALLOWED_USERS and not ALLOWED_GROUPS:
    raise SystemExit("ALLOWED_USER / ALLOWED_GROUP 至少填一个，否则 bot 不响应任何人")

X_COOKIES = os.path.expanduser(os.environ.get("X_COOKIES", "~/x-cookies.txt"))
DOUYIN_COOKIES = os.path.expanduser(os.environ.get("DOUYIN_COOKIES", "~/douyin-cookies.txt"))

# Pyrogram（MTProto）大文件发送：Bot API 上传硬限 50MB，配了 api_id/hash 则 >50MB 走 MTProto 直发
TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0") or "0")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
PYRO_ENABLED = bool(TELEGRAM_API_ID and TELEGRAM_API_HASH)
BOT_API_LIMIT_MB = 50
PYRO_LIMIT_MB = 1900
COMPRESS_MAX_SRC_MB = 200
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4000
ALBUM_CHUNK = 10
RETRIES = 3

_pyro = None
if PYRO_ENABLED:
    from pyrogram import Client as _PyroClient  # kurigram，import 名仍为 pyrogram

    _pyro = _PyroClient(
        "douyin_bot_pyro",
        api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH, bot_token=BOT_TOKEN,
        workdir=SAVE_DIR, no_updates=True,
    )

PLATFORMS = (
    "douyin.com", "v.douyin.com", "iesdouyin.com", "tiktok.com",
    "twitter.com", "x.com", "youtube.com", "youtu.be",
    "instagram.com", "weibo.com", "bilibili.com", "b23.tv", "kuaishou.com",
    "xiaohongshu.com", "xhslink.com",
)
# 这些平台下载失败就静默跳过（多半是需要登录/地区限制，报错没意义）
VIDEO_ONLY = (
    "youtube.com", "youtu.be", "bilibili.com", "b23.tv",
    "instagram.com", "kuaishou.com", "xiaohongshu.com", "xhslink.com",
)
DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")
X_HOSTS = ("twitter.com", "x.com")


# ---------------- Telegram 发送封装 ----------------
class SafeMessage:
    """包装 Message：原消息被删时 reply_* 回退为直发；网络错误重试。"""

    def __init__(self, msg):
        self._msg = msg
        self._bot = msg.get_bot()
        self._cid = msg.chat.id

    def __getattr__(self, name):
        return getattr(self._msg, name)

    async def _fallback(self, method, fallback, *args, **kwargs):
        for attempt in range(RETRIES):
            try:
                return await method(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                if "not found" in str(e).lower():
                    return await fallback(chat_id=self._cid, *args, **kwargs)
                is_net = isinstance(e, (NetworkError, TimedOut)) and not isinstance(e, BadRequest)
                if is_net and attempt < RETRIES - 1:
                    log.warning("网络重试 %d/%d: %s", attempt + 1, RETRIES - 1, str(e)[:120])
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise

    async def reply_text(self, *a, **kw):
        return await self._fallback(self._msg.reply_text, self._bot.send_message, *a, **kw)

    async def reply_video(self, *a, **kw):
        return await self._fallback(self._msg.reply_video, self._bot.send_video, *a, **kw)

    async def reply_photo(self, *a, **kw):
        return await self._fallback(self._msg.reply_photo, self._bot.send_photo, *a, **kw)

    async def reply_media_group(self, *a, **kw):
        return await self._fallback(self._msg.reply_media_group, self._bot.send_media_group, *a, **kw)


async def send_long_text(msg: SafeMessage, text: str) -> None:
    while text:
        await msg.reply_text(text[:TEXT_LIMIT])
        text = text[TEXT_LIMIT:]


async def send_photo_bytes(msg: SafeMessage, photos: Sequence[bytes], caption: str = "") -> None:
    """图片相册，按 10 张分组；caption 挂在最后一张。"""
    if not photos:
        return
    if len(photos) == 1:
        await msg.reply_photo(photo=photos[0], caption=caption or None)
        return
    groups = [photos[i:i + ALBUM_CHUNK] for i in range(0, len(photos), ALBUM_CHUNK)]
    for idx, group in enumerate(groups):
        media = [InputMediaPhoto(media=b) for b in group]
        if idx == len(groups) - 1 and caption:
            media[-1] = InputMediaPhoto(media=group[-1], caption=caption)
        await msg.reply_media_group(media=media)


def to_jpeg_if_webp(content: bytes, content_type: str, url: str) -> bytes:
    """Telegram 相册不收 webp，转 JPEG。"""
    if "webp" in content_type or ".webp" in url:
        buf = BytesIO()
        Image.open(BytesIO(content)).convert("RGB").save(buf, "JPEG", quality=90)
        return buf.getvalue()
    return content


def download_images(urls: Sequence[str], user_agent: str) -> list[bytes]:
    out: list[bytes] = []
    for u in urls:
        try:
            r = requests.get(u, timeout=30, headers={"User-Agent": user_agent})
            if r.status_code == 200:
                out.append(to_jpeg_if_webp(r.content, r.headers.get("content-type", ""), u))
        except Exception as e:  # noqa: BLE001
            log.warning("图片下载失败 %s: %s", u, e)
    return out


# ---------------- 视频处理 / 发送 ----------------
def _ffprobe(path: str, *entries: str) -> str:
    r = subprocess.run(
        ["ffprobe", "-v", "error", *entries, "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    return r.stdout.strip()


def ensure_h264(src: str) -> str:
    """非 h264/h265 编码的视频转成 h264，返回新路径；否则原路返回。"""
    codec = _ffprobe(src, "-select_streams", "v:0", "-show_entries", "stream=codec_name").lower()
    if codec in ("h264", "h265", "hevc", ""):
        return src
    dst = src.rsplit(".", 1)[0] + "_h264.mp4"
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-c:v", "libx264", "-preset", "fast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst],
        capture_output=True,
    )
    if r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
        return dst
    if os.path.exists(dst):
        os.remove(dst)
    return src


def compress_video(src: str, target_mb: float = 49.0) -> str:
    """压到 target_mb 以内；源超过 COMPRESS_MAX_SRC_MB 不压（会太糊），返回空串。"""
    if os.path.getsize(src) / (1024 * 1024) > COMPRESS_MAX_SRC_MB:
        return ""
    try:
        duration = float(_ffprobe(src, "-show_entries", "format=duration"))
    except ValueError:
        return ""
    video_kbps = max(int(target_mb * 8 * 1024 / duration) - 128, 200)
    dst = src.rsplit(".", 1)[0] + "_compressed.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-c:v", "libx264", "-b:v", f"{video_kbps}k",
         "-c:a", "aac", "-b:a", "128k", "-preset", "fast", "-movflags", "+faststart", dst],
        capture_output=True,
    )
    if os.path.exists(dst) and os.path.getsize(dst) < target_mb * 1024 * 1024:
        return dst
    if os.path.exists(dst):
        os.remove(dst)
    return ""


def video_dimensions(path: str) -> tuple[int, int]:
    out = _ffprobe(path, "-select_streams", "v:0", "-show_entries", "stream=width,height")
    parts = out.split()
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return int(parts[0]), int(parts[1])
    return 0, 0


def video_duration(path: str) -> int:
    try:
        return int(float(_ffprobe(path, "-show_entries", "format=duration")))
    except ValueError:
        return 0


async def run_subprocess(*args: str) -> subprocess.CompletedProcess:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: subprocess.run(list(args), capture_output=True, text=True)
    )


async def send_video(msg: SafeMessage, video_path: str, caption: str) -> bool:
    """>50MB 且配了 Pyrogram → MTProto 原画；否则压到 50MB 内走 Bot API。原片保留，副本清理。"""
    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    use_pyro = PYRO_ENABLED and size_mb > BOT_API_LIMIT_MB
    if use_pyro and size_mb > PYRO_LIMIT_MB:
        await msg.reply_text(f"⚠️ 视频过大（{size_mb:.1f}MB），超过 {PYRO_LIMIT_MB}MB 上限\n📁 {video_path}")
        return False
    send_path = video_path
    if size_mb > BOT_API_LIMIT_MB and not use_pyro:
        send_path = await asyncio.to_thread(compress_video, video_path)
        if not send_path:
            await msg.reply_text(
                f"⚠️ 视频过大（{size_mb:.1f}MB），超过 {COMPRESS_MAX_SRC_MB}MB 不压缩；"
                f"配置 TELEGRAM_API_ID/HASH 可直发 2GB 内原画\n📁 {video_path}"
            )
            return False
    converted = await asyncio.to_thread(ensure_h264, send_path)
    if converted != send_path:
        if send_path != video_path:
            os.remove(send_path)
        send_path = converted
    w, h = video_dimensions(send_path)
    caption = caption[:CAPTION_LIMIT]
    try:
        if use_pyro:
            await _pyro.send_video(
                chat_id=msg.chat.id, video=send_path, caption=caption,
                width=w, height=h, duration=video_duration(send_path),
                supports_streaming=True, reply_to_message_id=msg.message_id,
            )
        else:
            with open(send_path, "rb") as vf:
                await msg.reply_video(video=vf, width=w or None, height=h or None,
                                      caption=caption, supports_streaming=True)
    finally:
        if send_path != video_path and os.path.exists(send_path):
            os.remove(send_path)
    return True


# ---------------- 平台处理 ----------------
def build_caption(title: str, url: str, label: str = "视频标题") -> str:
    head = f"{label}：{title}\n\n" if title else ""
    return f"{head}🔗 {url}"


async def handle_douyin(msg: SafeMessage, url: str, video_path: str, mode: str) -> None:
    clean_url = url
    try:
        _, _, clean_url = await asyncio.to_thread(douyin.resolve_video_id, url)
    except Exception as e:  # noqa: BLE001
        log.warning("抖音短链解析失败，用原链接继续: %s", e)
    item: douyin.DouyinItem | None = None
    last_err = ""
    for attempt in range(RETRIES):
        try:
            item = await asyncio.to_thread(douyin.fetch_item, clean_url)
            break
        except douyin.DouyinUnavailable as e:
            await msg.reply_text(f"❌ {e}\n🔗 {clean_url}")
            return
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            log.warning("抖音解析重试 %d/%d: %s", attempt + 1, RETRIES, e)
            if attempt < RETRIES - 1:
                await asyncio.sleep(2 * (attempt + 1))

    if item is None:
        # 兜底：yt-dlp 直接下抖音页面（有 cookie 更稳）
        cookie_args = ["--cookies", DOUYIN_COOKIES] if os.path.exists(DOUYIN_COOKIES) else []
        dl = await run_subprocess("yt-dlp", "--no-playlist", *cookie_args, "-o", video_path, clean_url)
        if dl.returncode == 0 and os.path.exists(video_path) and os.path.getsize(video_path) > 0:
            await send_video(msg, video_path, build_caption("", clean_url))
            return
        hint = "（图文帖请手动保存图片）" if "/note/" in clean_url else ""
        await msg.reply_text(f"❌ 抖音解析失败{hint}：{last_err[:200]}\n🔗 {clean_url}")
        return

    if item.kind == "images":
        if mode == "title_only":
            await msg.reply_text(build_caption(item.title, clean_url, "图文标题"))
            return
        photos = await asyncio.to_thread(download_images, item.images, douyin.SHARE_HEADERS["User-Agent"])
        if not photos:
            await msg.reply_text(f"❌ 未能下载到图片，请手动保存\n{build_caption(item.title, clean_url, '图文标题')}")
            return
        await send_photo_bytes(msg, photos, build_caption(item.title, clean_url, "图文标题")[:CAPTION_LIMIT])
        return

    if mode == "text_only":
        await send_long_text(msg, build_caption(item.title, clean_url))
        return
    ok = await asyncio.to_thread(douyin.download_stream, item.video_url, video_path)
    if not ok:
        dl = await run_subprocess("yt-dlp", "--no-playlist", "-o", video_path, item.video_url)
        ok = dl.returncode == 0 and os.path.exists(video_path) and os.path.getsize(video_path) > 0
    if not ok:
        await msg.reply_text(f"❌ 抖音视频下载失败\n🔗 {clean_url}")
        return
    await send_video(msg, video_path, build_caption(item.title, clean_url))


async def handle_x(msg: SafeMessage, url: str, video_path: str, mode: str) -> None:
    """X：视频走 FxTwitter 直链；无视频则发 推文原文 + 配图。全程不转文案。"""
    prefix = f"{SAVE_DIR}/x_{abs(hash(url))}"
    info = await asyncio.to_thread(fx_twitter.fetch_x_content, url, prefix)
    if info is None:
        await msg.reply_text(f"❌ 推文解析失败（FxTwitter 无数据）\n🔗 {url}")
        return
    text = info["text"]
    if info["title"]:
        text = f"{info['title']}\n\n{text}".strip()
    if info["quote"] and info["quote"]["text"]:
        q = info["quote"]
        text = f"{text}\n\n———— 引用 @{q['user']}：\n{q['text']}".strip()
    body = f"📝 推文内容：\n{text}\n\n🔗 {url}" if text else f"🔗 {url}"

    def cleanup_images() -> None:
        for p in info["images"]:
            try:
                os.remove(p)
            except OSError:
                pass

    if mode == "text_only":
        cleanup_images()
        await send_long_text(msg, body)
        return

    vinfo = await asyncio.to_thread(fx_twitter.fetch_video_info, url)
    video_ok = False
    if vinfo:
        video_ok = await asyncio.to_thread(fx_twitter.download_video, vinfo["url"], video_path)
    if not video_ok:
        cookie_args = ["--cookies", X_COOKIES] if os.path.exists(X_COOKIES) else []
        dl = await run_subprocess("yt-dlp", "--no-playlist", *cookie_args, "-o", video_path, url)
        video_ok = dl.returncode == 0 and os.path.exists(video_path) and os.path.getsize(video_path) > 0

    if video_ok:
        cleanup_images()
        caption = body if mode != "title_only" else f"🔗 {url}"
        sent = await send_video(msg, video_path, caption)
        if sent and len(body) > CAPTION_LIMIT and mode != "title_only":
            await send_long_text(msg, body)
        return

    # 纯文字 / 图片推文
    photos: list[bytes] = []
    for p in info["images"]:
        try:
            with open(p, "rb") as f:
                photos.append(f.read())
        except OSError:
            pass
    cleanup_images()
    if photos:
        await send_photo_bytes(msg, photos, body if len(body) <= CAPTION_LIMIT else "")
        if len(body) > CAPTION_LIMIT:
            await send_long_text(msg, body)
    else:
        await send_long_text(msg, body)


def _read_info_json(video_path: str) -> tuple[str, str]:
    """yt-dlp --write-info-json 产物里取 title / description，读完删掉。"""
    info_path = os.path.splitext(video_path)[0] + ".info.json"
    if not os.path.exists(info_path):
        return "", ""
    try:
        with open(info_path, encoding="utf-8") as f:
            data = json.load(f)
        return (data.get("title") or "").strip(), (data.get("description") or "").strip()
    except (OSError, json.JSONDecodeError):
        return "", ""
    finally:
        try:
            os.remove(info_path)
        except OSError:
            pass


async def handle_generic(msg: SafeMessage, url: str, video_path: str, mode: str) -> None:
    """YouTube / B站 / TikTok / 快手 / Instagram / 微博：yt-dlp 下载 + 标题简介。"""
    dl = await run_subprocess(
        "yt-dlp", "--no-playlist", "--write-info-json", "--no-write-playlist-metafiles",
        "-o", video_path, url,
    )
    title, desc = _read_info_json(video_path)
    if dl.returncode != 0 or not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
        if os.path.exists(video_path):
            os.remove(video_path)
        if any(h in url for h in VIDEO_ONLY):
            log.info("静默跳过 %s: %s", url, (dl.stderr or "")[-200:])
            return
        await msg.reply_text(f"❌ 下载失败：{(dl.stderr or 'unknown')[-300:]}")
        return
    caption = build_caption(title, url)
    if mode == "text_only":
        text = caption if not desc else f"视频标题：{title}\n\n简介：\n{desc}\n\n🔗 {url}"
        await send_long_text(msg, text)
        return
    await send_video(msg, video_path, caption)
    if desc and mode != "title_only":
        await send_long_text(msg, f"简介：\n{desc}\n\n🔗 {url}")


async def process(msg: SafeMessage, url: str, mode: str) -> None:
    await msg.reply_text("⏳ 处理中，请稍候...")
    video_path = f"{SAVE_DIR}/video_{abs(hash(url))}.mp4"
    if any(h in url for h in DOUYIN_HOSTS):
        await handle_douyin(msg, url, video_path, mode)
    elif any(h in url for h in X_HOSTS):
        await handle_x(msg, url, video_path, mode)
    else:
        await handle_generic(msg, url, video_path, mode)


# ---------------- 消息入口 ----------------
def parse_mode(raw: str) -> str | None:
    """口令：/skip 跳过 → None；/title 标题 → title_only；/text 文案 → text_only；否则 default。"""
    if re.search(r"(/skip|跳过)", raw):
        return None
    if re.search(r"(/title|标题)", raw):
        return "title_only"
    if re.search(r"(/text|文案)", raw):
        return "text_only"
    return "default"


async def handle(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    user = update.message.from_user
    chat_id = update.message.chat.id
    log.info("收到消息 user=%s(%s) chat=%s", user.username or user.first_name, user.id, chat_id)
    if chat_id not in ALLOWED_GROUPS and user.id not in ALLOWED_USERS:
        return
    raw = update.message.text.strip()
    mode = parse_mode(raw)
    if mode is None:
        return
    m = re.search(r"https?://\S+", raw)
    if not m:
        return
    url = m.group(0).rstrip(".,)")
    if not any(p in url for p in PLATFORMS):
        return
    msg = SafeMessage(update.message)
    try:
        await process(msg, url, mode)
    except Exception as e:  # noqa: BLE001
        log.exception("处理失败 %s", url)
        try:
            await msg.reply_text(f"❌ 出错了：{str(e)[:300]}")
        except Exception:  # noqa: BLE001
            pass


# ---------------- 启动 / 健康检查 ----------------
_PROBE_VIDEO_ID = "7700146181942414642"  # 兜底探针（2026-10-31 在线）；正常由 feed API 现取


async def health_check_douyin(context: ContextTypes.DEFAULT_TYPE) -> None:
    """周检：feed API 拿一个在线视频 ID，再走正式解析路径（分享页）确认能拿到直链。"""
    if not BOT_OWNER:
        return
    try:
        probe_id = await asyncio.to_thread(douyin.fetch_live_probe_id) or _PROBE_VIDEO_ID
        item = await asyncio.to_thread(douyin.fetch_from_share_page, probe_id)
        text = f"✅ [周检] 抖音解析链路正常（{item.kind} {probe_id}）"
    except Exception as e:  # noqa: BLE001
        text = f"❌ [周检] 抖音解析链路异常：{e}"
    await context.bot.send_message(chat_id=BOT_OWNER, text=text)


async def on_startup(app) -> None:
    if _pyro is not None:
        await _pyro.start()
        log.info("Pyrogram（大文件发送）已连接")
    if BOT_OWNER:
        startup_msg = (
            "🤖 Douyin Bot（精简版）已启动\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "直接发链接 → 视频/图片 + 标题/原文\n"
            "加 标题 或 /title → 只发视频+标题\n"
            "加 文案 或 /text → 只发文字不发视频\n"
            "加 跳过 或 /skip → 忽略不处理\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "支持：抖音(视频/图文) / X / YouTube / B站 / TikTok 等\n"
            "不含：语音转文案、AI 梳理"
        )
        try:
            await app.bot.send_message(chat_id=BOT_OWNER, text=startup_msg)
        except Exception as e:  # noqa: BLE001
            log.warning("启动通知失败: %s", e)
    app.job_queue.run_repeating(health_check_douyin, interval=7 * 24 * 3600, first=10)


async def on_shutdown(_app) -> None:
    if _pyro is not None and _pyro.is_connected:
        await _pyro.stop()


def main() -> None:
    app = (
        ApplicationBuilder().token(BOT_TOKEN)
        .read_timeout(300).write_timeout(600).connect_timeout(60)
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT, handle))
    app.post_init = on_startup
    app.post_shutdown = on_shutdown
    log.info("Bot 启动中... SAVE_DIR=%s pyrogram=%s", SAVE_DIR, PYRO_ENABLED)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
