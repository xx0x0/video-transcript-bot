"""小红书 / rednote（国际版）笔记解析：视频直链 + 图文相册。

页面 www.xiaohongshu.com 与 www.rednote.com 是同一套前端，笔记数据内嵌在 window.__INITIAL_STATE__
（note.noteDetailMap[id].note），yt-dlp 的 XiaoHongShu 提取器也是读这个，但只认 xiaohongshu.com 且只处理视频。

登录态（2026-11-17 实测）：页面 guestBrowsePolicy 只放行 sources ["seo","direct"]，
App 分享出的 xhslink 短链（xsec_source=app_share）不登录能看；浏览器搜索流复制的链接
（xsec_source=pc_search）游客直接被甩到 /login，必须带登录 cookie。
cookie 文件 XHS_COOKIES（默认 ~/xhs-cookies.txt，Netscape 格式，用 "Get cookies.txt LOCALLY" 扩展在
已登录的 rednote.com / xiaohongshu.com 导出）。cookie 只对导出时的域名有效，所以不改写域名，
按用户发来的域名请求；撞登录墙再换另一个域名试一次（cookie 文件里可能是另一边的）。
"""
import http.cookiejar
import json
import os
import re
from typing import NamedTuple

import requests

XHS_COOKIES = os.path.expanduser(os.environ.get("XHS_COOKIES") or "~/xhs-cookies.txt")
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
PAGE_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 300
_HOSTS = ("xiaohongshu.com", "rednote.com", "xhslink.com")


class NeedLogin(Exception):
    """页面被甩到登录页 / 没有笔记数据：游客不可见，需要 cookie"""


class Note(NamedTuple):
    kind: str          # "video" | "images"
    title: str
    desc: str
    images: tuple      # urlDefault 列表（图文帖）
    video_url: str     # masterUrl（视频帖）


def is_xhs_url(url: str) -> bool:
    return any(h in url for h in _HOSTS)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    if os.path.exists(XHS_COOKIES):
        jar = http.cookiejar.MozillaCookieJar(XHS_COOKIES)
        jar.load(ignore_discard=True, ignore_expires=True)
        s.cookies = jar
    return s


def _swap_host(url: str) -> str:
    if "rednote.com" in url:
        return url.replace("www.rednote.com", "www.xiaohongshu.com").replace("rednote.com", "www.xiaohongshu.com")
    return url.replace("www.xiaohongshu.com", "www.rednote.com")


def _parse_initial_state(html: str) -> dict:
    i = html.find("window.__INITIAL_STATE__")
    if i < 0:
        raise NeedLogin("页面没有 __INITIAL_STATE__")
    raw = html[html.index("=", i) + 1:]
    raw = raw.split("</script>", 1)[0].strip().rstrip(";")
    # 页面是 JS 字面量，含 undefined，用 yt-dlp 的转换器转成合法 JSON
    from yt_dlp.utils import js_to_json
    return json.loads(js_to_json(raw))


def _fetch_once(s: requests.Session, url: str) -> Note:
    m = re.search(r"/(?:explore|discovery/item|item)/([0-9a-f]{24})", url)
    if not m:
        raise ValueError(f"链接里找不到笔记 ID：{url}")
    note_id = m.group(1)
    r = s.get(url, timeout=PAGE_TIMEOUT, allow_redirects=True)
    if "/login" in r.url:
        raise NeedLogin("被跳转到登录页")
    state = _parse_initial_state(r.text)
    note = (((state.get("note") or {}).get("noteDetailMap") or {}).get(note_id) or {}).get("note")
    if not note:
        raise NeedLogin("页面无笔记数据（游客不可见）")
    title = (note.get("title") or "").strip()
    desc = (note.get("desc") or "").strip()
    images = tuple(
        u for u in (img.get("urlDefault") for img in note.get("imageList") or []) if u
    )
    video_url = ""
    streams = ((note.get("video") or {}).get("media") or {}).get("stream") or {}
    # 优先 h264（Telegram 直接能播），再退其他编码
    for codec in ("h264", "h265", "h266", "av1"):
        for info in streams.get(codec) or []:
            u = info.get("masterUrl") or next(iter(info.get("backupUrls") or []), "")
            if u:
                video_url = u
                break
        if video_url:
            break
    if note.get("type") == "video" and video_url:
        return Note("video", title, desc, (), video_url)
    if images:
        return Note("images", title, desc, images, "")
    raise RuntimeError(f"笔记既无视频地址也无图片（type={note.get('type')}）")


def fetch_note(url: str) -> Note:
    """解析笔记。撞登录墙时换另一个域名再试一次（cookie 可能是另一边的）；仍不行抛 NeedLogin。"""
    s = _session()
    try:
        return _fetch_once(s, url)
    except NeedLogin as first:
        alt = _swap_host(url)
        if alt == url:
            raise
        try:
            return _fetch_once(s, alt)
        except NeedLogin:
            raise first


def download_stream(video_url: str, dest: str) -> bool:
    """CDN 直链流式下载，不需要 cookie。"""
    try:
        with requests.get(video_url, stream=True, timeout=DOWNLOAD_TIMEOUT,
                          headers={"User-Agent": _UA, "Referer": "https://www.xiaohongshu.com/"}) as r:
            if r.status_code != 200:
                print(f"[xhs download] HTTP {r.status_code}")
                return False
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if chunk:
                        f.write(chunk)
        return os.path.getsize(dest) > 0
    except Exception as e:
        print(f"[xhs download] {e}")
        if os.path.exists(dest):
            os.remove(dest)
        return False
