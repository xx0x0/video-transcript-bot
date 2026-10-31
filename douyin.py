"""抖音解析（无浏览器、无登录）。

2026-10-31 实测：
- 移动端 feed API（aweme.snssdk.com/aweme/v1/feed/?aweme_id=）已不认 aweme_id，每次返回随机推荐流，
  用它解析会下到无关视频。只留作「拿一个在线视频 ID」给周检探针用。
- 可用路径：requests 带 session 抓 iesdouyin 分享页，第一次响应种下 ttwid cookie，第二次起
  HTML 内嵌的 window._ROUTER_DATA 里就有 videoInfoRes.item_list[0]（视频直链 / 图文原图 / 标题）。
  直链把 /playwm/ 换成 /play/ 即无水印。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger("douyin")

SHARE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) EdgiOS/121.0.2277.107 Version/17.0 Mobile/15E148 Safari/604.1"
    )
}
FEED_API_HEADERS = {
    "User-Agent": (
        "com.ss.android.ugc.aweme/110101 (Linux; U; Android 10; zh_CN; Pixel 4; "
        "Build/QQ3A.200805.001; Cronet/58.0.2991.0)"
    ),
}
FEED_API_URL = "https://aweme.snssdk.com/aweme/v1/feed/"
SHARE_PAGE = "https://www.iesdouyin.com/share/{kind}/{video_id}"
TIMEOUT = 15
SHARE_PAGE_PASSES = 4       # 首次只种 cookie，一般第 2 次就有数据；多给几次余量
SHARE_PAGE_PAUSE = 0.8


@dataclass(frozen=True)
class DouyinItem:
    kind: str                       # "video" | "images"
    video_id: str
    title: str
    video_url: str = ""
    images: tuple[str, ...] = field(default_factory=tuple)


class DouyinUnavailable(ValueError):
    """内容被过滤（地区限制 / 已删除 / 仅好友可见）。"""


def resolve_video_id(url: str) -> tuple[str, str, str]:
    """返回 (aweme_id, kind, 干净链接)。kind 是 'video' 或 'note'；短链会跟随跳转。"""
    m = re.search(r"/(video|note|slides)/(\d+)", url)
    if not m:
        r = requests.get(url, headers=SHARE_HEADERS, timeout=TIMEOUT, allow_redirects=True)
        m = re.search(r"/(video|note|slides)/(\d+)", r.url)
        if not m:
            tail = r.url.split("?")[0].strip("/").split("/")[-1]
            if not tail.isdigit():
                raise ValueError(f"无法从跳转结果解析视频 ID: {r.url}")
            return tail, "video", f"https://www.douyin.com/video/{tail}"
    kind = "video" if m.group(1) == "video" else "note"
    return m.group(2), kind, f"https://www.douyin.com/{kind}/{m.group(2)}"


def _clean_title(desc: str, video_id: str) -> str:
    desc = (desc or "").strip() or f"douyin_{video_id}"
    return re.sub(r'[\\/:*?"<>|]', "_", desc)


def _router_data_from_html(html: str) -> dict | None:
    idx = html.find("window._ROUTER_DATA")
    if idx < 0:
        return None
    eq = html.find("=", idx)
    if eq < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html[eq + 1:].lstrip())
    except json.JSONDecodeError as e:
        log.warning("_ROUTER_DATA JSON 解析失败: %s", e)
        return None
    return data if isinstance(data, dict) else None


def _find_video_info_res(data: dict) -> dict | None:
    """loaderData 下 'video_(id)/page' / 'note_(id)/page' 之类的页面数据里取 videoInfoRes。"""
    for page in (data.get("loaderData") or {}).values():
        if isinstance(page, dict) and isinstance(page.get("videoInfoRes"), dict):
            return page["videoInfoRes"]
    return None


def _item_to_douyin(item: dict, video_id: str) -> DouyinItem:
    title = _clean_title(item.get("desc", ""), video_id)
    images = item.get("images")
    if images:
        urls: list[str] = []
        for im in images:
            lst = im.get("url_list") if isinstance(im, dict) else None
            if lst and lst[0] not in urls:
                urls.append(lst[0])
        if urls:
            return DouyinItem("images", video_id, title, images=tuple(urls))
    video = item.get("video") or {}
    bit_rate = video.get("bit_rate") or []
    addr = (bit_rate[0].get("play_addr") if bit_rate else None) or video.get("play_addr") or {}
    url_list = addr.get("url_list") or []
    if not url_list:
        raise ValueError(f"分享页数据里没有视频地址 (id={video_id})")
    return DouyinItem("video", video_id, title, video_url=url_list[0].replace("/playwm/", "/play/"))


def fetch_from_share_page(video_id: str, kind: str = "video") -> DouyinItem:
    """抓 iesdouyin 分享页，多次请求直到 _ROUTER_DATA 里出现 item_list。"""
    session = requests.Session()
    session.headers.update(SHARE_HEADERS)
    url = SHARE_PAGE.format(kind=kind, video_id=video_id)
    last_state = "未拿到 _ROUTER_DATA"
    for attempt in range(SHARE_PAGE_PASSES):
        r = session.get(url, timeout=TIMEOUT)
        data = _router_data_from_html(r.text)
        res = _find_video_info_res(data) if data else None
        if res is None:
            last_state = f"第 {attempt + 1} 次无 videoInfoRes"
        else:
            items = res.get("item_list") or []
            if items:
                item = items[0]
                if str(item.get("aweme_id") or video_id) != str(video_id):
                    raise ValueError(f"分享页返回的 aweme_id={item.get('aweme_id')} 与请求不符")
                return _item_to_douyin(item, video_id)
            if res.get("filter_list"):
                raise DouyinUnavailable("内容不可用（地区限制 / 已删除 / 仅好友可见）")
            last_state = f"第 {attempt + 1} 次 item_list 为空"
        if attempt < SHARE_PAGE_PASSES - 1:
            time.sleep(SHARE_PAGE_PAUSE)
    raise ValueError(f"抖音分享页解析失败：{last_state} (id={video_id})")


def fetch_item(url: str) -> DouyinItem:
    """解析抖音链接。视频页先按 video 抓，拿不到再按 note 抓一次（部分图文用 /video/ 链接分享）。"""
    video_id, kind, _ = resolve_video_id(url)
    try:
        return fetch_from_share_page(video_id, kind)
    except DouyinUnavailable:
        raise
    except ValueError as e:
        other = "note" if kind == "video" else "video"
        log.info("按 %s 解析失败（%s），改按 %s 再试", kind, e, other)
        return fetch_from_share_page(video_id, other)


def fetch_live_probe_id() -> str | None:
    """feed API 已不认 aweme_id，但仍返回在线视频；借它拿一个当前存在的 ID 做链路探针。"""
    try:
        r = requests.get(FEED_API_URL, params={"aweme_id": "1"}, headers=FEED_API_HEADERS, timeout=TIMEOUT)
        lst = r.json().get("aweme_list") or []
        return str(lst[0]["aweme_id"]) if lst else None
    except Exception as e:  # noqa: BLE001
        log.warning("feed API 探针失败: %s", e)
        return None


def download_stream(url: str, save_path: str, timeout: int = 120) -> bool:
    """流式下载视频直链。失败清理残留并返回 False。"""
    try:
        with requests.get(url, headers=SHARE_HEADERS, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(save_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
        if os.path.getsize(save_path) > 0:
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("抖音视频下载失败 %s: %s", url, e)
    try:
        os.remove(save_path)
    except OSError:
        pass
    return False
