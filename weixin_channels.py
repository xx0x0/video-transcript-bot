"""微信视频号下载：走第三方上游，两个上游轮换（任一失败自动切另一个）。

视频号的视频地址只下发给微信登录态，微信外的预览接口只有封面/文案（2026-11-01 实测），
yt-dlp 也没有提取器。这两家服务端自己持有微信会话，返回 finder.video.qq.com 的明文 mp4
（720p h264/aac，实测直接 GET 可播，无需解密）。

上游 1 snapany 官方 API（anyfetcher 的真实源；.env 配了 SNAPANY_API_KEY 才启用）：
    POST https://api.snapany.com/openapi/v1/extract/post  {"url": 链接}
    头 Authorization: Bearer sk_snapany_xxx
    → {"title"/"text": ..., "medias": [{"media_type": "video", "resource_url": ..., "headers": {...}}]}
    计费：1 积分/条，成功才扣；新号送 50 积分，9000 积分 ¥69。Key 在 platform.snapany.com/zh/console/keys
    错误：401 invalid_api_key，402 insufficient_credits，429 rate_limited，400 + code（unsupported_site 等，不扣费）
上游 2 anyfetcher（免费、无 key，匿名约 2 次/天/IP）：
    POST https://anyfetcher.com/api/video/parse  {"url": 链接}
    → {"title": ..., "medias": [...同上]}；429 DAILY_LIMIT_EXCEEDED=当日额度用完

有 key 先走 snapany，失败切 anyfetcher；没 key 只有 anyfetcher。全失败抛 UpstreamError，消息列出各家原因。
直链：用户自己在 snapany 网页解析后复制的 finder.video.qq.com 地址，发给 bot 直接下载，不走上游不占额度。
"""
import os
import re
import requests

PARSE_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 300
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
SNAPANY_API_KEY = os.environ.get("SNAPANY_API_KEY", "").strip()


class UpstreamError(Exception):
    """上游不可用 / 返回异常，message 直接给用户看"""


def is_channels_url(url: str) -> bool:
    return bool(re.search(r"weixin\.qq\.com/sph/|channels\.weixin\.qq\.com|finder\.video\.qq\.com", url))


def is_direct_video_url(url: str) -> bool:
    """finder.video.qq.com/.../stodownload?...：用户自己在 snapany 等网页解析出来的临时 mp4 地址（带 token/sign，几小时过期），
    直接下载不经任何上游、不占额度"""
    return "finder.video.qq.com" in url


def normalize_url(url: str) -> str:
    """channels.weixin.qq.com/finder-preview/pages/sph?id=XXX → https://weixin.qq.com/sph/XXX（上游认短链）"""
    m = re.search(r"channels\.weixin\.qq\.com/.*[?&]id=([A-Za-z0-9_-]+)", url)
    if m:
        return f"https://weixin.qq.com/sph/{m.group(1)}"
    return url.split("#")[0]


def _parse_json(r: requests.Response) -> dict:
    try:
        return r.json()
    except ValueError as e:
        raise UpstreamError("返回的不是 JSON，接口可能已改") from e


def _pick_video(data: dict) -> tuple[str, dict, str]:
    """从上游 JSON 取 (视频地址, 下载时要带的头, 标题)；没视频抛 UpstreamError"""
    videos = [m for m in data.get("medias") or [] if m.get("media_type") == "video" and m.get("resource_url")]
    if not videos:
        raise UpstreamError(f"没解析出视频（{str(data)[:100]}）")
    title = (data.get("title") or data.get("text") or "").strip()
    return videos[0]["resource_url"], videos[0].get("headers") or {}, title


def _parse_snapany(share_url: str) -> tuple[str, dict, str]:
    r = requests.post("https://api.snapany.com/openapi/v1/extract/post", json={"url": share_url},
                      headers={"Authorization": f"Bearer {SNAPANY_API_KEY}"}, timeout=PARSE_TIMEOUT)
    if r.status_code == 200:
        return _pick_video(_parse_json(r))
    try:
        code = r.json().get("code") or ""
    except ValueError:
        code = ""
    if r.status_code == 401:
        raise UpstreamError(f"API Key 无效（{code}），检查 .env 的 SNAPANY_API_KEY")
    if r.status_code == 402:
        raise UpstreamError("积分用完（insufficient_credits），去 platform.snapany.com 充值")
    if r.status_code == 400:
        raise UpstreamError(f"上游判定不能提取：{code or r.text[:80]}")
    raise UpstreamError(f"HTTP {r.status_code} {code or r.text[:80]}")


def _parse_anyfetcher(share_url: str) -> tuple[str, dict, str]:
    r = requests.post("https://anyfetcher.com/api/video/parse", json={"url": share_url},
                      headers={"User-Agent": _UA}, timeout=PARSE_TIMEOUT)
    if r.status_code == 429:
        raise UpstreamError("今日免费额度用完（HTTP 429）")
    if r.status_code != 200:
        raise UpstreamError(f"HTTP {r.status_code} {r.text[:80]}")
    return _pick_video(_parse_json(r))


def upstreams() -> tuple:
    """按优先级返回 (名字, 解析函数)：有 key 先 snapany，再 anyfetcher"""
    chain = [("anyfetcher", _parse_anyfetcher)]
    if SNAPANY_API_KEY:
        chain.insert(0, ("snapany", _parse_snapany))
    return tuple(chain)


def _download(video_url: str, extra_headers: dict, dest: str) -> None:
    headers = {"User-Agent": _UA, **extra_headers}
    try:
        with requests.get(video_url, headers=headers, stream=True, timeout=DOWNLOAD_TIMEOUT) as dl:
            if dl.status_code != 200:
                raise UpstreamError(f"视频地址下载失败 HTTP {dl.status_code}（上游给的地址可能已过期）")
            with open(dest, "wb") as f:
                for chunk in dl.iter_content(1024 * 256):
                    f.write(chunk)
    except requests.RequestException as e:
        raise UpstreamError(f"视频下载中断：{e.__class__.__name__}") from e
    # 视频号原始流开头是加密的；上游给的应是明文 mp4，不是就说明微信改了加密、上游没跟上
    with open(dest, "rb") as f:
        head = f.read(16)
    if b"ftyp" not in head:
        os.remove(dest)
        raise UpstreamError("下到的文件不是 mp4（微信可能改了加密，上游未跟上）")


def fetch_channels_video(url: str, dest: str) -> str:
    """依次试每个上游解析，拿到地址就下载到 dest，返回标题；全失败抛 UpstreamError 列出各家原因。"""
    if is_direct_video_url(url):
        print("[weixin_channels] 直链，跳过上游直接下载")
        _download(url, {}, dest)
        return ""
    share_url = normalize_url(url)
    failures = []
    for name, parse in upstreams():
        try:
            video_url, headers, title = parse(share_url)
        except UpstreamError as e:
            failures.append(f"{name}：{e}")
            print(f"[weixin_channels] {name} 失败，切下一个：{e}")
            continue
        except requests.RequestException as e:
            failures.append(f"{name}：连不上（{e.__class__.__name__}）")
            print(f"[weixin_channels] {name} 连不上，切下一个：{e}")
            continue
        print(f"[weixin_channels] {name} 解析成功")
        _download(video_url, headers, dest)
        return title
    hint = "" if SNAPANY_API_KEY else "\n（只配了免费上游；.env 加 SNAPANY_API_KEY 可启用 snapany 官方 API，1 积分/条）"
    raise UpstreamError("上游全部失败\n" + "\n".join(f"• {f}" for f in failures) + hint)
