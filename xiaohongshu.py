"""小红书 / rednote（国际版）笔记解析：视频直链 + 图文相册。

页面 www.xiaohongshu.com 与 www.rednote.com 是同一套前端，笔记数据内嵌在 window.__INITIAL_STATE__
（note.noteDetailMap[id].note），yt-dlp 的 XiaoHongShu 提取器也是读这个，但只认 xiaohongshu.com 且只处理视频。

游客访问（2026-11-17 实测）：裸 HTTP 请求（无 cookie）会被 302 到 /login，但无头 Chromium 不登录就能打开笔记页，
因为游客 cookie（a1 / webId / web_session / websectiga ...）是页面 JS 生成的，requests 拿不到。
所以顺序：先 requests（最快，App 分享的 xhslink 链接能直接过）→ 撞登录墙换 Playwright 无头浏览器加载再读
window.__INITIAL_STATE__（VPS 装了截图依赖就有）。都拿不到才抛 NeedLogin。
可选 XHS_COOKIES（默认 ~/xhs-cookies.txt，Netscape 格式）：有就带上，给只对登录用户可见的内容用；正常不需要。
cookie 只对导出时的域名有效，所以不改写域名，按用户发来的域名请求；撞登录墙再换另一个域名试一次。
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
BROWSER_TIMEOUT_MS = 30000
BROWSER_SETTLE_MS = 3000
DOWNLOAD_TIMEOUT = 300
_HOSTS = ("xiaohongshu.com", "rednote.com", "xhslink.com")
_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item|item)/([0-9a-f]{24})")
# 浏览器里直接取 noteDetailMap[id].note，避免把整个 state 序列化回来
_JS_PICK_NOTE = """(id) => {
  const s = window.__INITIAL_STATE__ || {};
  const m = (s.note && s.note.noteDetailMap) || {};
  const n = (m[id] && m[id].note) || null;
  if (!n) return null;
  return {type: n.type, title: n.title, desc: n.desc,
          imageList: (n.imageList || []).map(i => ({urlDefault: i.urlDefault})),
          video: {media: {stream: ((n.video || {}).media || {}).stream || {}}}};
}"""


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


def _note_id(url: str) -> str:
    m = _NOTE_ID_RE.search(url)
    if not m:
        raise ValueError(f"链接里找不到笔记 ID：{url}")
    return m.group(1)


def _fetch_once(s: requests.Session, url: str) -> Note:
    note_id = _note_id(url)
    r = s.get(url, timeout=PAGE_TIMEOUT, allow_redirects=True)
    if "/login" in r.url:
        raise NeedLogin("被跳转到登录页")
    state = _parse_initial_state(r.text)
    note = (((state.get("note") or {}).get("noteDetailMap") or {}).get(note_id) or {}).get("note")
    if not note:
        raise NeedLogin("页面无笔记数据（游客不可见）")
    return _note_from_dict(note)


def _fetch_via_browser(url: str) -> Note:
    """无头 Chromium 打开笔记页，页面 JS 自己生成游客 cookie，再读 __INITIAL_STATE__。"""
    from playwright.sync_api import sync_playwright
    note_id = _note_id(url)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            # 不要自定义 UA：指定桌面 Chrome UA 会被甩到 /login，Playwright 默认 UA 反而能过（2026-11-17 对照实测 3 次）
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
            except Exception as e:
                print(f"[xhs browser goto] {e}")
            page.wait_for_timeout(BROWSER_SETTLE_MS)
            if "/login" in page.url:
                raise NeedLogin("浏览器也被跳转到登录页")
            note = page.evaluate(_JS_PICK_NOTE, note_id)
        finally:
            browser.close()
    if not note:
        raise NeedLogin("浏览器页面无笔记数据")
    return _note_from_dict(note)


def _note_from_dict(note: dict) -> Note:
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
    """解析笔记：requests（原域名 → 换域名）→ 无头浏览器。全部撞墙抛 NeedLogin（带最后一步的原因）。"""
    s = _session()
    candidates = [url]
    alt = _swap_host(url)
    if alt != url:
        candidates.append(alt)
    last: Exception = NeedLogin("未尝试")
    for u in candidates:
        try:
            return _fetch_once(s, u)
        except NeedLogin as e:
            last = e
    print(f"[xhs] requests 撞登录墙（{last}），改用无头浏览器")
    try:
        return _fetch_via_browser(url)
    except ImportError:
        raise NeedLogin(f"{last}；且本机没装 Playwright，无法用浏览器兜底")


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
