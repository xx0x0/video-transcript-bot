"""FxTwitter API 拉取 X 推文/长推/文章内容。

X 自 2026-08 起对 headless 浏览器一律返回 HTTP 403 空白页（带不带登录 cookie 都拦），
Playwright 的 X 截图/提取链路全部失效。改走 FxTwitter 公共解析 API：
GET api.fxtwitter.com/i/status/<id> 返回 JSON——长推（NoteTweet）text 即完整全文，
X 文章正文在 article.content.blocks，图片/视频给 pbs/video.twimg 直链，
全程无需浏览器和 cookie。
"""
import json
import os
import re
import urllib.request
from typing import Optional

API_URL = "https://api.fxtwitter.com/i/status/{tid}"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
MAX_IMAGES = 10
DEFAULT_TIMEOUT = 15
# 选档上限：默认 Bot API 上传上限 50MB 留余量到 48MB；
# 配了 TELEGRAM_API_ID/HASH（走 Pyrogram/MTProto 发送）时放宽到 1900MB，可选 720p 原画档
_PYRO_AVAILABLE = bool(os.environ.get("TELEGRAM_API_ID") and os.environ.get("TELEGRAM_API_HASH"))
TG_MAX_VIDEO_BYTES = (1900 if _PYRO_AVAILABLE else 48) * 1024 * 1024


def _extract_tweet_id(url: str) -> Optional[str]:
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else None


def _api_get(tid: str, timeout: int) -> Optional[dict]:
    req = urllib.request.Request(
        API_URL.format(tid=tid), headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data.get("tweet") if data.get("code") == 200 else None


def _article_text(article: dict) -> str:
    """文章正文：content.blocks 逐块拼接，空时退回 preview_text。"""
    blocks = ((article.get("content") or {}).get("blocks")) or []
    text = "\n\n".join(b.get("text", "") for b in blocks).strip()
    return text or (article.get("preview_text") or "").strip()


def _image_urls(tweet: dict) -> list:
    urls = []
    article = tweet.get("article")
    if article:
        cover = ((article.get("cover_media") or {}).get("media_info") or {}).get("original_img_url")
        if cover:
            urls.append(cover)
    media = tweet.get("media") or {}
    photos = list(media.get("photos") or [])
    if not photos:
        photos = [m for m in (media.get("all") or []) if m.get("type") == "photo"]
    for ph in photos:
        u = ph.get("url")
        if u and u not in urls:
            urls.append(u)
    return urls


def _download_images(urls: list, save_path_prefix: str, timeout: int) -> list:
    paths = []
    for idx, u in enumerate(urls[:MAX_IMAGES]):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            path = f"{save_path_prefix}_img{idx + 1}.jpg"
            with open(path, "wb") as f:
                f.write(body)
            paths.append(path)
        except Exception as e:
            print(f"[fx 下载图片失败] {u}: {e}")
    return paths


def fetch_full_text(url: str, timeout: int = DEFAULT_TIMEOUT) -> Optional[str]:
    """推文完整文本（NoteTweet 长推的 text 即全文；文章返回 标题+正文）。失败返回 None。"""
    tid = _extract_tweet_id(url)
    if not tid:
        return None
    try:
        tweet = _api_get(tid, timeout)
    except Exception as e:
        print(f"[fx api 失败] {url}: {e}")
        return None
    if not tweet:
        return None
    text = (tweet.get("text") or "").strip()
    article = tweet.get("article")
    if article and not text:
        title = (article.get("title") or "").strip()
        text = f"{title}\n\n{_article_text(article)}".strip()
    return text or None


def fetch_video_info(url: str, timeout: int = DEFAULT_TIMEOUT) -> Optional[dict]:
    """X 视频：FxTwitter 拿 video.twimg.com 的 mp4 直链（免鉴权，不走 yt-dlp）。
    多档码率里选「预估体积不超 TG 上限的最高档」，全超则取最低档。
    返回 {'url': str, 'title': str}；无视频/API 失败返回 None。
    """
    tid = _extract_tweet_id(url)
    if not tid:
        return None
    try:
        tweet = _api_get(tid, timeout)
    except Exception as e:
        print(f"[fx api 失败] {url}: {e}")
        return None
    if not tweet:
        return None

    media = tweet.get("media") or {}
    videos = list(media.get("videos") or [])
    if not videos:
        videos = [m for m in (media.get("all") or []) if m.get("type") in ("video", "gif")]
    if not videos:
        return None

    v = videos[0]
    duration = float(v.get("duration") or 0)
    mp4s = [f for f in (v.get("formats") or [])
            if f.get("container") == "mp4" and f.get("url")]
    if mp4s:
        mp4s.sort(key=lambda f: int(f.get("bitrate") or 0), reverse=True)
        best = next(
            (f["url"] for f in mp4s
             if not duration or int(f.get("bitrate") or 0) * duration / 8 <= TG_MAX_VIDEO_BYTES),
            mp4s[-1]["url"],
        )
    else:
        best = v.get("url")
    if not best:
        return None
    return {"url": best, "title": (tweet.get("text") or "").strip()}


def download_video(url: str, save_path: str, timeout: int = 120) -> bool:
    """流式下载视频直链到本地。失败清理残留文件，返回 False。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(save_path, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        if os.path.getsize(save_path) > 0:
            return True
    except Exception as e:
        print(f"[fx 视频下载失败] {url}: {e}")
    try:
        os.remove(save_path)
    except Exception:
        pass
    return False


def fetch_x_content(url: str, save_path_prefix: str, timeout: int = DEFAULT_TIMEOUT) -> Optional[dict]:
    """返回与 bot.extract_page_content 相同结构的 dict：
      kind: 'x_article' | 'x_quote' | 'x_tweet'
      title: str   文章标题；其余为空
      text:  str   主推正文（长推为全文；文章为正文全文）
      images: list[str]  已下载到本地的图片路径
      quote: dict | None  {'text': str, 'user': str}
    API 失败/非 status 链接返回 None。
    """
    tid = _extract_tweet_id(url)
    if not tid:
        return None
    try:
        tweet = _api_get(tid, timeout)
    except Exception as e:
        print(f"[fx api 失败] {url}: {e}")
        return None
    if not tweet:
        return None

    text = (tweet.get("text") or "").strip()
    article = tweet.get("article")
    quoted = tweet.get("quote")
    title = ""
    quote = None

    if article:
        kind = "x_article"
        title = (article.get("title") or "").strip()
        text = _article_text(article) or text
    elif quoted:
        kind = "x_quote"
        q_article = quoted.get("article")
        if q_article:
            q_text = f"{(q_article.get('title') or '').strip()}\n\n{_article_text(q_article)}".strip()
        else:
            q_text = (quoted.get("text") or "").strip()
        q_author = quoted.get("author") or {}
        quote = {
            "text": q_text,
            "user": q_author.get("screen_name") or "",
            "name": q_author.get("name") or "",
        }
    else:
        kind = "x_tweet"

    author = tweet.get("author") or {}
    images = _download_images(_image_urls(tweet), save_path_prefix, timeout)
    return {
        "kind": kind, "title": title, "text": text, "images": images, "quote": quote,
        "author": {
            "name": author.get("name") or "",
            "screen_name": author.get("screen_name") or "",
            "avatar": author.get("avatar_url") or "",
        },
        "created_timestamp": tweet.get("created_timestamp"),
    }
