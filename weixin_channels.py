"""微信视频号下载：走第三方上游 anyfetcher.com。

视频号的视频地址只下发给微信登录态，微信外的预览接口只有封面/文案（2026-11-01 实测），
yt-dlp 也没有提取器。anyfetcher 服务端自己持有微信会话，公开接口：
    POST https://anyfetcher.com/api/video/parse  {"url": "<分享链接>"}
    → {"title": ..., "medias": [{"media_type": "video", "resource_url": "<finder.video.qq.com mp4>"}]}
resource_url 可直接 GET，拿到的是正常 mp4（720p h264/aac，实测无需解密）。
限制：匿名额度很低（实测同一 IP 当日第 3 次就 429 DAILY_LIMIT_EXCEEDED）；上游随时可能失效，
失效时抛 UpstreamError，bot 原样回给用户。
"""
import os
import re
import requests

UPSTREAM = "https://anyfetcher.com/api/video/parse"
PARSE_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 300
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class UpstreamError(Exception):
    """上游不可用 / 返回异常，message 直接给用户看"""


def is_channels_url(url: str) -> bool:
    return bool(re.search(r"weixin\.qq\.com/sph/|channels\.weixin\.qq\.com", url))


def normalize_url(url: str) -> str:
    """channels.weixin.qq.com/finder-preview/pages/sph?id=XXX → https://weixin.qq.com/sph/XXX（上游认短链）"""
    m = re.search(r"channels\.weixin\.qq\.com/.*[?&]id=([A-Za-z0-9_-]+)", url)
    if m:
        return f"https://weixin.qq.com/sph/{m.group(1)}"
    return url.split("#")[0]


def fetch_channels_video(url: str, dest: str) -> str:
    """解析并下载到 dest，返回标题。失败抛 UpstreamError。"""
    share_url = normalize_url(url)
    try:
        r = requests.post(UPSTREAM, json={"url": share_url},
                          headers={"User-Agent": _UA, "Referer": "https://anyfetcher.com/zh-cn/tools/weixin"},
                          timeout=PARSE_TIMEOUT)
    except requests.RequestException as e:
        raise UpstreamError(f"上游 anyfetcher 连不上：{e.__class__.__name__}") from e

    if r.status_code == 429:
        raise UpstreamError("上游 anyfetcher 今日解析额度已用完（匿名限额），明天再试")
    if r.status_code != 200:
        raise UpstreamError(f"上游 anyfetcher 返回 HTTP {r.status_code}：{r.text[:120]}")
    try:
        data = r.json()
    except ValueError as e:
        raise UpstreamError("上游 anyfetcher 失效：返回的不是 JSON（接口可能已改）") from e

    videos = [m for m in data.get("medias") or [] if m.get("media_type") == "video" and m.get("resource_url")]
    if not videos:
        raise UpstreamError(f"上游 anyfetcher 失效：没解析出视频（{str(data)[:120]}）")
    title = (data.get("title") or "").strip()

    try:
        with requests.get(videos[0]["resource_url"], headers={"User-Agent": _UA},
                          stream=True, timeout=DOWNLOAD_TIMEOUT) as dl:
            if dl.status_code != 200:
                raise UpstreamError(f"视频地址下载失败 HTTP {dl.status_code}（上游给的地址可能已过期）")
            with open(dest, "wb") as f:
                for chunk in dl.iter_content(1024 * 256):
                    f.write(chunk)
    except requests.RequestException as e:
        raise UpstreamError(f"视频下载中断：{e.__class__.__name__}") from e

    # 视频号原始流开头是加密的；上游给的应是明文 mp4，不是就说明上游变了
    with open(dest, "rb") as f:
        head = f.read(16)
    if b"ftyp" not in head:
        os.remove(dest)
        raise UpstreamError("上游 anyfetcher 失效：下到的文件不是 mp4（微信可能改了加密，上游未跟上）")
    return title
