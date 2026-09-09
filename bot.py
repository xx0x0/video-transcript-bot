#!/usr/bin/env python3
import subprocess, os, sys, json, re, requests, glob, asyncio
from telegram import Update, InputMediaPhoto
from telegram.error import NetworkError, TimedOut, BadRequest
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from playwright.sync_api import sync_playwright
from PIL import Image

# 从环境变量读取（由 run.sh 加载 .env 注入）
BOT_TOKEN = os.environ["BOT_TOKEN"]
BADNEWS_COOKIES = os.environ.get("BADNEWS_COOKIES", "")

SAVE_DIR = os.path.expanduser("~/Downloads/抖音")
DOUYIN_MCP = os.path.expanduser("~/douyin-mcp-server")
os.makedirs(SAVE_DIR, exist_ok=True)

# 白名单：只响应指定用户私聊 + 指定群
ALLOWED_USERS = {int(x) for x in os.environ["ALLOWED_USER"].split(",") if x.strip()}
ALLOWED_GROUPS = {int(x) for x in os.environ["ALLOWED_GROUP"].split(",") if x.strip()}
BOT_OWNER = int(os.environ.get("BOT_OWNER", "0")) or (min(ALLOWED_USERS) if ALLOWED_USERS else 0)

# ---- pkb 归档（失败不影响 bot 主流程）----
PKB_DIR = os.environ.get("PKB_DIR", os.path.expanduser("~/pkb"))
sys.path.insert(0, os.path.join(PKB_DIR, "tools"))
try:
    from telegram_archive import archive_clip
except Exception as _e:
    archive_clip = None
    print(f"[pkb] 归档模块未加载，本次不落盘: {_e}")

PLATFORMS = [
    "douyin.com", "v.douyin.com", "tiktok.com", "xiaohongshu.com",
    "xhslink.com", "twitter.com", "x.com", "youtube.com", "youtu.be",
    "instagram.com", "weibo.com", "bilibili.com", "b23.tv", "kuaishou.com",
    "news.qq.com", "view.inews.qq.com", "bad.news", "github.com",
]
VIDEO_ONLY = (
    "youtube.com", "youtu.be", "bilibili.com", "b23.tv",
    "instagram.com", "kuaishou.com", "xiaohongshu.com", "xhslink.com",
)

sys.path.insert(0, DOUYIN_MCP)
from douyin_mcp_server.server import get_douyin_download_link

# X 已删光 data-testid（2026-08 实测），旧的按 testid 隐藏弹窗的 CSS 失配；
# 改按 fixed/sticky 定位 + 关键词清除 cookie 同意弹窗、登录/注册悬浮条。
# 文本超 600 字的不删，防止误删包住整页的容器。
X_OVERLAY_CLEANUP_JS = """() => {
    document.querySelectorAll('div,section,aside').forEach(el => {
        const cs = getComputedStyle(el);
        if (cs.position !== 'fixed' && cs.position !== 'sticky') return;
        const t = (el.innerText || '').trim();
        if (!t || t.length > 600) return;
        if (/cookies?|log ?in|sign ?up|登录|注册/i.test(t)) el.remove();
    });
}"""


def webpage_screenshot(url, save_path_prefix, max_segments=8):
    """用 playwright 滚动分段截图，返回 (图片路径列表, 页面标题)

    每段一张 viewport 大小的清晰图，便于 Telegram 显示时不被压糊。
    X/Twitter 会额外隐藏侧栏和登录弹窗。
    """
    paths = []
    is_x = ("twitter.com" in url) or ("x.com" in url)

    with sync_playwright() as p:
        # X 对 headless 浏览器一律返回 403 空白页（2026-08 起），X 改走有头模式，窗口移到屏幕外
        browser = p.chromium.launch(
            headless=not is_x,
            args=["--window-position=-32000,-32000"] if is_x else [],
        )
        # X 用窄视口触发响应式布局，侧栏不渲染；其他站点保持宽视口
        vp_width = 700 if is_x else 1200
        context = browser.new_context(
            viewport={"width": vp_width, "height": 1600},
            device_scale_factor=2,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        if is_x:
            try:
                from x_long_tweet import _load_x_cookies
                ck = _load_x_cookies()
                if ck:
                    context.add_cookies(ck)
            except Exception as e:
                print(f"[X cookie load failed] {e}")
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("load", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        title = page.title() or ""

        # 仅 X/Twitter：隐藏侧栏 + 登录弹窗，并强制允许滚动
        if is_x:
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
            except Exception:
                pass
            page.add_style_tag(content="""
                [data-testid="sidebarColumn"],
                [data-testid="BottomBar"],
                [data-testid="sheetDialog"],
                [data-testid="mask"],
                [aria-label*="ign up" i],
                [aria-label*="og in" i],
                div[role="dialog"][aria-modal="true"] { display: none !important; }
                html, body { overflow: auto !important; height: auto !important; }
            """)
            try:
                page.locator('article[data-testid="tweet"]').first.wait_for(timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(800)
            try:
                page.evaluate(X_OVERLAY_CLEANUP_JS)
            except Exception:
                pass

        # 先滚动一遍触发懒加载
        total_height = page.evaluate("document.body.scrollHeight")
        viewport_height = page.evaluate("window.innerHeight")
        for scroll_y in range(0, total_height, viewport_height):
            page.evaluate(f"window.scrollTo(0, {scroll_y})")
            page.wait_for_timeout(400)
        # 回到顶部，重新测量高度（懒加载可能改变总高）
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
        total_height = page.evaluate("document.body.scrollHeight")

        cut_css_y = None
        full_path = None
        if is_x:
            # 截图前清场（一次性执行）：
            # 1) 删掉所有 fixed/sticky 元素——顶部悬浮条、cookie 同意框（逐屏截图时会盖在每段顶部）
            # 2) 摘掉嵌在正文中间的 "See all the replies"/"Continue to X" 登录拦截卡
            #    （向上爬到整卡容器再删，高度>400 或文本>200 字视为爬过头）
            # 3) 删完重新量主文（第一个 <article>，testid 删光后普通标签仍在）bottom
            #    作为截图终点：评论区/推荐内容一律不进截图
            try:
                cut_css_y = page.evaluate("""() => {
                    document.querySelectorAll('div,section,aside,header,nav').forEach(el => {
                        const cs = getComputedStyle(el);
                        if (cs.position === 'fixed' || cs.position === 'sticky') el.remove();
                    });
                    for (const el of document.querySelectorAll('div,section,a,span')) {
                        const t = (el.innerText || '').trim();
                        if (!t || t.length > 80) continue;
                        if (!/^(see all the replies|continue to x)/i.test(t)) continue;
                        let box = el;
                        for (let i = 0; i < 6; i++) {
                            const p = box.parentElement;
                            if (!p) break;
                            const r = p.getBoundingClientRect();
                            const pt = (p.innerText || '').trim();
                            if (r.height > 400 || pt.length > 200) break;
                            box = p;
                        }
                        box.remove();
                    }
                    const art = document.querySelector('article');
                    if (!art) return null;
                    const r = art.getBoundingClientRect();
                    return r.bottom + window.scrollY;
                }""")
                page.wait_for_timeout(300)
            except Exception:
                cut_css_y = None

            # X 逐屏滚动截图。不用 full_page：整页截图会把视口拉成整页高度，
            # 触发 X 重新渲染 → 内容漂移出现重复/错位（文章一长就复现）。
            # 逐屏截视口所见即所得；每段按「目标区间 − 实际 scrollY」用 PIL 裁剪，
            # 底部滚动钳制也不会重叠或遗漏。
            dpr = 2
            end_css = int(cut_css_y or page.evaluate("document.body.scrollHeight"))
            end_css = min(end_css, viewport_height * max_segments)
            y = 0
            idx = 0
            while y < end_css and idx < max_segments:
                page.evaluate(f"window.scrollTo(0, {y})")
                page.wait_for_timeout(400)
                actual = int(page.evaluate("window.scrollY"))
                seg_path = f"{save_path_prefix}_{idx+1}.png"
                page.screenshot(path=seg_path)
                top_c = max(0, y - actual)
                bot_c = min(viewport_height, min(y + viewport_height, end_css) - actual)
                if bot_c - top_c < 40:  # 有效内容不足 40 CSS 像素，丢弃
                    try: os.remove(seg_path)
                    except Exception: pass
                    break
                if (top_c, bot_c) != (0, viewport_height):
                    seg_img = Image.open(seg_path)
                    seg_img.crop((0, top_c * dpr, seg_img.size[0], bot_c * dpr)).save(seg_path)
                    seg_img.close()
                paths.append(seg_path)
                idx += 1
                y += viewport_height
        else:
            # 非 X：整页截图，后面用 PIL 精确切分（零重叠零遗漏）
            full_path = f"{save_path_prefix}_full.png"
            page.screenshot(path=full_path, full_page=True)

        # 提取页面中的内容图片（过滤掉头像/图标等小图）
        # X 跳过：截图分段里已含全部配图，再附原图会导致相册里同图出现两遍
        image_urls = [] if is_x else page.evaluate("""() => {
            const imgs = document.querySelectorAll('img');
            const urls = [];
            const seen = new Set();
            for (const img of imgs) {
                const src = img.src || img.getAttribute('data-src') || '';
                if (!src || src.startsWith('data:')) continue;
                // 跳过头像、图标等小图（自然尺寸 < 200px）
                if (img.naturalWidth > 0 && img.naturalWidth < 200) continue;
                if (img.naturalHeight > 0 && img.naturalHeight < 200) continue;
                // X/Twitter 内容图片特征
                const isXMedia = src.includes('pbs.twimg.com/media');
                // 通用内容图片：尺寸足够大
                const isBigEnough = img.naturalWidth >= 400 || img.naturalHeight >= 400;
                if ((isXMedia || isBigEnough) && !seen.has(src)) {
                    seen.add(src);
                    urls.push(src);
                }
            }
            return urls;
        }""")

        # 下载提取到的图片（原图，发送时排在截图分段之后）
        img_paths = []
        for idx, img_url in enumerate(image_urls[:10]):
            try:
                img_path = f"{save_path_prefix}_img{idx+1}.jpg"
                resp = page.request.get(img_url)
                if resp.ok:
                    with open(img_path, "wb") as f:
                        f.write(resp.body())
                    img_paths.append(img_path)
            except Exception as e:
                print(f"[提取图片失败] {img_url}: {e}")

        browser.close()

    # 非 X：PIL 切分整页截图（device_scale_factor=2，实际像素是 CSS 像素的 2 倍）
    # X 的分段截图已在上面逐屏生成，不走这里
    if full_path:
        dpr = 2
        seg_pixel_h = viewport_height * dpr  # 每段像素高度 = 视口高 × DPR
        try:
            full_img = Image.open(full_path)
            fw, fh = full_img.size
            if fh <= seg_pixel_h:
                # 整页不超过一个视口，直接作为一张
                seg_path = f"{save_path_prefix}_1.png"
                full_img.save(seg_path)
                paths.insert(0, seg_path)
            else:
                idx = 0
                for top in range(0, fh, seg_pixel_h):
                    bottom = min(fh, top + seg_pixel_h)
                    # 最后一片太薄（< 15% 视口），合并到上一片
                    if idx > 0 and (bottom - top) < seg_pixel_h * 0.15:
                        break
                    crop = full_img.crop((0, top, fw, bottom))
                    seg_path = f"{save_path_prefix}_{idx+1}.png"
                    crop.save(seg_path)
                    paths.append(seg_path)
                    idx += 1
                    if idx >= max_segments:
                        break
            full_img.close()
        except Exception as e:
            print(f"[PIL 切分失败，回退整图] {e}")
            paths.insert(0, full_path)
            full_path = None  # 不删除
        if full_path:
            try:
                os.remove(full_path)
            except Exception:
                pass

    # 顺序固定：截图分段 1..N 在前，原图在后
    # （修复：原先原图先入列表、多段截图 append 在其后，导致相册里原图夹在前面像重复乱序）
    return paths + img_paths, title


def is_article_url(url):
    """白名单作为快速判定：明确的文章平台直接走截图，不再用 yt-dlp 探测节省时间"""
    ARTICLE_HOSTS = [
        "twitter.com", "x.com",
        "mp.weixin.qq.com", "weixin.qq.com",
        "weibo.com",
        "zhihu.com",
        "medium.com",
        "substack.com",
    ]
    return any(h in url for h in ARTICLE_HOSTS)


def has_video(url):
    """用 yt-dlp 探测 URL 是否包含可下载视频。10 秒超时。"""
    try:
        r = subprocess.run(
            ["yt-dlp", "--simulate", "--no-warnings", "--quiet",
             "--no-playlist", "--print", "id", url],
            capture_output=True, text=True, timeout=10
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def normalize_for_telegram(paths):
    """确保图片符合 Telegram 限制：宽高比 1:20~20:1、尺寸≤10000，超则切分/缩放。
    返回处理后的路径列表（可能比输入多）。
    """
    MAX_SIDE = 8000              # 单边像素上限（保守值）
    MAX_RATIO = 18               # 宽高比上限（保守值，实际 Telegram 是 20）
    TARGET_WIDTH = 1600          # 切分后每段目标宽度
    result = []
    for p in paths:
        try:
            img = Image.open(p)
            w, h = img.size
            # 先按单边上限整体缩放
            if max(w, h) > MAX_SIDE:
                scale = MAX_SIDE / max(w, h)
                w2, h2 = int(w * scale), int(h * scale)
                img = img.resize((w2, h2), Image.LANCZOS)
                img.save(p)
                w, h = w2, h2
            # 再查宽高比：太长就竖切成多段
            if h / max(w, 1) > MAX_RATIO:
                # 每段高度 = 宽度 × (MAX_RATIO - 2)，留余量
                seg_h = int(w * (MAX_RATIO - 3))
                n = (h + seg_h - 1) // seg_h
                base, ext = os.path.splitext(p)
                for i in range(n):
                    top = i * seg_h
                    bot = min(h, top + seg_h)
                    crop = img.crop((0, top, w, bot))
                    new_path = f"{base}_part{i+1}{ext}"
                    crop.save(new_path)
                    result.append(new_path)
                # 原图切分后删除
                try:
                    os.remove(p)
                except Exception:
                    pass
            else:
                result.append(p)
        except Exception as e:
            print(f"[图片规范化失败] {p}: {e}")
            result.append(p)
    return result


def clean_hallucination(text):
    """清理 whisper 幻觉：全局占比过高直接返回空，否则从末尾往前裁尾部幻觉。"""
    if not text:
        return text
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return text

    foreign_pat = re.compile(
        r"[\u0400-\u04FF"
        r"\uAC00-\uD7AF"
        r"\u3040-\u30FF"
        r"\u0600-\u06FF"
        r"\u0E00-\u0E7F"
        r"\u0370-\u03FF"
        r"]"
    )
    halluc_phrases = re.compile(
        r"优优独播剧场|YoYo Television|字幕志愿者|中文字幕|请不吝点赞|订阅.*转发|打赏支持"
    )

    def is_halluc_line(line):
        line = line.strip()
        if not line:
            return True
        if halluc_phrases.search(line):
            return True
        if foreign_pat.search(line):
            return True
        zh = len(re.findall(r"[\u4e00-\u9fff]", line))
        if len(line) <= 6 and zh <= 1:
            return True
        if re.fullmatch(r"[\s\d\.\?\!\u3002\uff1f\uff01\uff0c,\u2026\xb7\-A-Za-z]{1,6}", line):
            return True
        return False

    halluc_count = sum(1 for l in lines if is_halluc_line(l))
    if halluc_count / len(lines) > 0.5 or halluc_count > 20:
        return ""

    cut_idx = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if is_halluc_line(lines[i]):
            cut_idx = i
            continue
        break
    return "\n".join(lines[:cut_idx]).strip()


def _call_ollama(prompt: str, timeout: int = 120) -> str:
    import urllib.request, json as _json
    data = _json.dumps({"model": "qwen2.5:7b", "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read()).get("response", "").strip()
    except:
        return ""


def analyze_transcript(transcript, title):
    prompt = f"""你是内容提炼助手，严格基于原文内容，按以下格式输出：

**🎯 核心结论**
用2-3句话说明最重要的观点。

**📊 关键数据/事实**
列出具体数字或事实（没有则跳过）

**✅ 可执行建议**
1. 具体行动，动词开头
2. 具体行动，动词开头
3. 具体行动，动词开头

**⚠️ 避坑提醒**
需要注意的事项

**💬 一句话带走**
最值得记住的一句话，不超过20字

标题：{title}
文案：{transcript}"""
    return _call_ollama(prompt)


def analyze_brief(text, title):
    """简短梳理：3-5 句话核心要点。专有名词必须原样保留，便于复制搜索。"""
    prompt = f"""基于正文写 3-5 句要点，简洁。

强制规则：
- 网址、网站名、GitHub 项目名、产品名、工具名、书名、人名、英文专有名词、hashtag —— 一律原样照抄，不翻译、不改写、不缩写、不省略大小写
- 提到具体平台/工具/网站时，直接用原名称（如 NewsNow、今日热榜、SoPilot、GitHub Trending、Product Hunt）
- 不要"该文章"、"本帖"、"作者表示"这类元叙述
- 不要分段标题、emoji、星号、装饰符号
- 句式短，可用顿号或换行分隔

标题：{title}
正文：{text}"""
    return _call_ollama(prompt)


class SafeMessage:
    """包装 Message，所有 reply_* 调用在原消息被删时自动回退到直发"""
    def __init__(self, msg):
        self._msg = msg
        self._bot = msg.get_bot()
        self._cid = msg.chat.id

    def __getattr__(self, name):
        return getattr(self._msg, name)

    async def _fallback(self, method, fallback, *args, **kwargs):
        import asyncio
        for attempt in range(3):
            try:
                return await method(*args, **kwargs)
            except Exception as e:
                err = str(e).lower()
                if "not found" in err:
                    return await fallback(chat_id=self._cid, *args, **kwargs)
                # 只重试真正的网络错误：NetworkError/TimedOut，但排除 BadRequest（脏输入）
                is_net = isinstance(e, (NetworkError, TimedOut)) and not isinstance(e, BadRequest)
                if is_net and attempt < 2:
                    print(f"[网络重试 {attempt+1}/2] {type(e).__name__}: {str(e)[:120]}")
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


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    # 白名单检查
    user = update.message.from_user
    chat_id = update.message.chat.id
    print(f"收到消息 - 用户：{user.username or user.first_name}（ID:{user.id}）群：{chat_id}")
    if chat_id not in ALLOWED_GROUPS and user.id not in ALLOWED_USERS:
        return

    # 包装 message，原消息被删时所有 reply_* 自动回退到直发
    msg = SafeMessage(update.message)

    raw = msg.text.strip()

    # 口令解析（口令可在消息任意位置）
    # 跳过：/skip 或 跳过 → 不处理
    # 标题：/title 或 标题 → 只发视频+标题，不转文案
    # 文案：/text 或 文案 → 只发文案，不发视频
    mode = "default"
    if re.search(r"(/skip|跳过)", raw):
        return
    elif re.search(r"(/title|标题)", raw):
        mode = "title_only"
    elif re.search(r"(/text|文案)", raw):
        mode = "text_only"
    url_match = re.search(r"https?://\S+", raw)
    if not url_match:
        return
    text = url_match.group(0).rstrip(".,)")

    # 已知视频平台直接走视频流程
    is_known_platform = any(x in text for x in PLATFORMS)

    # 未知链接：白名单文章直接截图；其余先 yt-dlp 探测，有视频走下载，没视频截图
    if not is_known_platform:
        if is_article_url(text):
            try:
                await _process_article(msg, text)
            except Exception as e:
                print(f"[ERROR article] {e}")
                import traceback; traceback.print_exc()
                try:
                    await msg.reply_text(f"❌ 文章截图失败：{e}")
                except:
                    pass
            return

        loop = asyncio.get_event_loop()
        if await loop.run_in_executor(None, has_video, text):
            try:
                await _process(msg, text, mode=mode)
            except Exception as e:
                print(f"[ERROR unknown video] {e}")
                import traceback; traceback.print_exc()
                try:
                    await _process_article(msg, text)
                except Exception as e2:
                    await msg.reply_text(f"❌ 处理失败：{e}")
            return

        try:
            await _process_article(msg, text)
        except Exception as e:
            print(f"[ERROR fallback article] {e}")
            import traceback; traceback.print_exc()
            try:
                await msg.reply_text(f"❌ 链接处理失败：{e}")
            except:
                pass
        return

    # 解析短链接，去除追踪参数
    clean_url = text
    if "v.douyin.com" in text or "b23.tv" in text:
        try:
            r = requests.head(text, allow_redirects=True, timeout=10)
            real_url = r.url
            match = re.search(r"/video/(\d+)|/note/(\d+)", real_url)
            if match:
                vid = match.group(1) or match.group(2)
                path = "video" if match.group(1) else "note"
                clean_url = f"https://www.douyin.com/{path}/{vid}"
        except:
            pass

    try:
        await _process(msg, clean_url, mode=mode)
    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback; traceback.print_exc()
        try:
            await msg.reply_text(f"❌ 出错了：{e}")
        except:
            pass

def extract_page_content(url, save_path_prefix):
    """提取页面内容并分类。返回 dict:
      kind: 'x_article' | 'x_quote' | 'x_tweet' | 'generic'
      title: str   X 文章用文章标题；其他用 page.title()
      text:  str   主要正文
      images: list[str]  本地图片路径
      quote: dict | None  X 引用转推: {'text': str, 'user': str}
    """
    is_x = ("twitter.com" in url) or ("x.com" in url)
    paths = []
    title = ""
    text = ""
    quote = None
    kind = "generic"

    with sync_playwright() as p:
        # X 对 headless 浏览器一律返回 403 空白页（2026-08 起），X 改走有头模式，窗口移到屏幕外
        browser = p.chromium.launch(
            headless=not is_x,
            args=["--window-position=-32000,-32000"] if is_x else [],
        )
        vp_width = 700 if is_x else 1200
        context = browser.new_context(
            viewport={"width": vp_width, "height": 1600},
            device_scale_factor=2,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        if is_x:
            try:
                from x_long_tweet import _load_x_cookies
                ck = _load_x_cookies()
                if ck:
                    context.add_cookies(ck)
            except Exception as e:
                print(f"[X cookie load failed] {e}")
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("load", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2500)
        page_title_default = (page.title() or "").strip()

        if is_x:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            try:
                page.locator(
                    'article[data-testid="tweet"], [data-testid="twitterArticleRichTextView"]'
                ).first.wait_for(timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(800)

            classify = page.evaluate("""() => {
                const isArticle = !!document.querySelector('[data-testid="twitter-article-title"], [data-testid="twitterArticleRichTextView"]');
                const arts = document.querySelectorAll('article[data-testid="tweet"]');
                const main = arts[0];
                const texts = [];
                const users = [];
                if (main) {
                    main.querySelectorAll('[data-testid="tweetText"]').forEach(t => texts.push(t.innerText));
                    main.querySelectorAll('[data-testid="User-Name"]').forEach(u => users.push(u.innerText));
                }
                let articleTitle = '', articleBody = '';
                if (isArticle) {
                    const t = document.querySelector('[data-testid="twitter-article-title"]');
                    const b = document.querySelector('[data-testid="twitterArticleRichTextView"]');
                    articleTitle = t ? t.innerText : '';
                    articleBody = b ? b.innerText : '';
                }
                return { isArticle, texts, users, articleTitle, articleBody };
            }""") or {"isArticle": False, "texts": [], "users": [], "articleTitle": "", "articleBody": ""}

            if classify["isArticle"]:
                kind = "x_article"
                title = (classify["articleTitle"] or "").strip() or page_title_default
                text = (classify["articleBody"] or "").strip()
            elif len(classify["texts"]) >= 2:
                kind = "x_quote"
                text = (classify["texts"][0] or "").strip()
                quote_text = (classify["texts"][1] or "").strip()
                quote_user = ""
                if len(classify["users"]) >= 2:
                    # User-Name 格式 "显示名\n@handle\n· 时间"，取第一行
                    quote_user = (classify["users"][1] or "").split("\n")[0].strip()

                # X 在引用 card 里只渲染前 ~140 字，要拿完整版得另开一页抓被引用推的原 URL
                try:
                    main_id_m = re.search(r'/status/(\d+)', url)
                    main_id = main_id_m.group(1) if main_id_m else ""
                    quoted_target = page.evaluate(r"""(mainId) => {
                        const main = document.querySelector('article[data-testid="tweet"]');
                        if (!main) return '';
                        const seen = new Set();
                        for (const a of main.querySelectorAll('a[href*="/status/"]')) {
                            const h = a.getAttribute('href') || '';
                            const m = h.match(/^\/([\w]+)\/status\/(\d+)/);
                            if (!m) continue;
                            if (m[2] === mainId) continue;
                            const key = m[1] + '/' + m[2];
                            if (seen.has(key)) continue;
                            seen.add(key);
                            return key;
                        }
                        return '';
                    }""", main_id) or ""
                    if quoted_target and "/" in quoted_target:
                        q_handle, q_tid = quoted_target.split("/", 1)
                        quoted_url = f"https://x.com/{q_handle}/status/{q_tid}"
                        # X 对同 context 重复请求会推登录墙；独立 context 复刻匿名首访
                        qctx = browser.new_context(
                            viewport={"width": vp_width, "height": 1600},
                            device_scale_factor=2,
                            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                        )
                        qpage = qctx.new_page()
                        try:
                            qpage.goto(quoted_url, wait_until="domcontentloaded", timeout=30000)
                            try:
                                qpage.locator('article[data-testid="tweet"] [data-testid="tweetText"]').first.wait_for(timeout=12000)
                            except Exception:
                                pass
                            qpage.wait_for_timeout(1500)
                            quoted_full = qpage.evaluate(r"""() => {
                                const t = document.querySelector('article[data-testid="tweet"] [data-testid="tweetText"]');
                                return t ? t.innerText : '';
                            }""") or ""
                            quoted_full = quoted_full.strip()
                            if quoted_full and len(quoted_full) > len(quote_text):
                                quote_text = quoted_full
                        except Exception as e:
                            print(f"[抓引用原推失败] {quoted_url}: {e}")
                        finally:
                            qctx.close()
                except Exception as e:
                    print(f"[解析引用推 URL 失败] {e}")

                quote = {"text": quote_text, "user": quote_user}
                title = page_title_default
            else:
                kind = "x_tweet"
                text = (classify["texts"][0] if classify["texts"] else "").strip()
                title = page_title_default
        else:
            kind = "generic"
            try:
                text = page.evaluate("""() => {
                    const sels = ['article', 'main', '[role="main"]', '.post-content', '.article-content', '#content'];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el && el.innerText && el.innerText.trim().length > 100) return el.innerText;
                    }
                    return document.body ? document.body.innerText : '';
                }""") or ""
            except Exception:
                text = ""
            text = text.strip()
            title = page_title_default

        # 触发懒加载，确保图片 src 都有
        try:
            total_h = page.evaluate("document.body.scrollHeight")
            vp_h = page.evaluate("window.innerHeight")
            for sy in range(0, total_h, vp_h):
                page.evaluate(f"window.scrollTo(0, {sy})")
                page.wait_for_timeout(250)
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass

        try:
            image_urls = page.evaluate("""() => {
                const imgs = document.querySelectorAll('img');
                const urls = [];
                const seen = new Set();
                for (const img of imgs) {
                    const src = img.src || img.getAttribute('data-src') || '';
                    if (!src || src.startsWith('data:')) continue;
                    if (img.naturalWidth > 0 && img.naturalWidth < 200) continue;
                    if (img.naturalHeight > 0 && img.naturalHeight < 200) continue;
                    const isXMedia = src.includes('pbs.twimg.com/media');
                    const isBigEnough = img.naturalWidth >= 400 || img.naturalHeight >= 400;
                    if ((isXMedia || isBigEnough) && !seen.has(src)) {
                        seen.add(src);
                        urls.push(src);
                    }
                }
                return urls;
            }""") or []
            for idx, img_url in enumerate(image_urls[:10]):
                try:
                    img_path = f"{save_path_prefix}_img{idx+1}.jpg"
                    resp = page.request.get(img_url)
                    if resp.ok:
                        with open(img_path, "wb") as f:
                            f.write(resp.body())
                        paths.append(img_path)
                except Exception as e:
                    print(f"[提取图片失败] {img_url}: {e}")
        except Exception:
            pass

        browser.close()
    return {"kind": kind, "title": title, "text": text, "images": paths, "quote": quote}


async def _send_media_with_caption(msg, image_paths, caption=""):
    """发送图片相册（按 10 张分组）。caption 为空字符串时不附文字。"""
    photos = []
    for p in image_paths:
        with open(p, "rb") as f:
            photos.append(f.read())
    if len(photos) == 1:
        if caption:
            await msg.reply_photo(photo=photos[0], caption=caption)
        else:
            await msg.reply_photo(photo=photos[0])
        return
    CHUNK = 10
    groups = [photos[i:i + CHUNK] for i in range(0, len(photos), CHUNK)]
    for idx, group in enumerate(groups):
        media = [InputMediaPhoto(media=b) for b in group]
        if idx == len(groups) - 1 and caption:
            media[-1] = InputMediaPhoto(media=group[-1], caption=caption)
        await msg.reply_media_group(media=media)


async def _send_long_text(msg, text):
    while text:
        await msg.reply_text(text[:4000])
        text = text[4000:]


async def _send_screenshot_album(msg, ss_paths, title, url, summary=""):
    """发送截图分段相册，caption 放 标题+AI梳理+链接；
    梳理塞不进 1024 字 caption 时，相册后单发一条文字。发完清理文件。"""
    ss_paths = normalize_for_telegram(ss_paths)
    short_title = title[:200] if title else ""
    title_line = f"📄 {short_title}\n\n" if short_title else ""
    link_line = f"🔗 {url}"
    summary_block = f"📝 AI 梳理：\n{summary}\n\n" if summary else ""
    cap = title_line + summary_block + link_line
    overflow = ""
    if len(cap) > 1024:
        cap = (title_line + link_line)[:1024]
        overflow = f"📝 AI 梳理：\n{summary}"
    try:
        await _send_media_with_caption(msg, ss_paths, cap)
        if overflow:
            await _send_long_text(msg, overflow)
    finally:
        for p in ss_paths:
            try: os.remove(p)
            except Exception: pass


async def _screenshot_with_summary(msg, loop, url, prefix, summary_source, title):
    """截图模式：webpage_screenshot 给截图分段+原图，caption 只放 标题+链接。
    截图本身就是原文，不做 AI 梳理。
    summary_source 参数保留以便日后想加可选梳理时复用。
    """
    ss_paths, _ = await loop.run_in_executor(None, webpage_screenshot, url, prefix)
    ss_paths = [p for p in ss_paths if os.path.exists(p)]
    if not ss_paths:
        await msg.reply_text(f"❌ 截图失败\n🔗 {url}")
        return
    await _send_screenshot_album(msg, ss_paths, title, url)


# 主+引合计字数超过这个就走截图，避免 TG 对话框堆长文
SCREENSHOT_THRESHOLD = 1000


async def _process_article(msg, url: str):
    """文章/推文链接分流：
      x_article:               截图+原图 + 标题 + 链接
      x_quote, 合计 > 1000:    截图+原图 + 标题 + 链接
      x_quote, 合计 ≤ 1000:    搬运 主推 + ———— 引用 @user：+ 引用文 + 图 + 链接
      x_tweet, > 1000:         截图+原图 + 标题 + 链接
      x_tweet, ≤ 1000:         搬运 推文 + 图 + 链接
      generic:                 搬运 标题 + 文本 + 图 + 链接（不截图）
    """
    await msg.reply_text("⏳ 处理中，请稍候...")
    prefix = f"{SAVE_DIR}/article_{abs(hash(url))}"
    loop = asyncio.get_event_loop()
    is_x = ("twitter.com" in url) or ("x.com" in url)

    # X 页面已删光 data-testid（2026-08 实测），DOM 分类失效；
    # 改用 FxTwitter API 拿 kind/正文/引用/图，截图走本地渲染卡片（x_card）
    from_fx = False
    if is_x:
        from fx_twitter import fetch_x_content
        info = await loop.run_in_executor(None, fetch_x_content, url, prefix)
        from_fx = info is not None
        if info is None:
            info = await loop.run_in_executor(None, extract_page_content, url, prefix)
    else:
        info = await loop.run_in_executor(None, extract_page_content, url, prefix)
    kind = info["kind"]
    text = info["text"]
    title = info["title"]
    images = info["images"]
    quote = info["quote"]

    # 决定走截图还是搬运
    if kind == "x_article":
        do_screenshot = True
    elif kind == "x_quote" and quote and quote["text"]:
        combined_len = len(text) + len(quote["text"])
        do_screenshot = combined_len > SCREENSHOT_THRESHOLD
    elif kind == "x_tweet":
        do_screenshot = len(text) > SCREENSHOT_THRESHOLD
    else:
        do_screenshot = False

    if do_screenshot:
        # X 走本地渲染卡片：FxTwitter 数据 → 本地 HTML → headless 整页截图 + PIL 切分
        # （一次渲染一刀切，无重复/遮挡；x.com 封 headless 也不受影响）
        if from_fx:
            from x_card import render_card
            ss_paths = await loop.run_in_executor(None, render_card, info, prefix)
            ss_paths = [p for p in ss_paths if os.path.exists(p)]
            if ss_paths:
                for p in images:
                    try: os.remove(p)
                    except Exception: pass
                # AI 短梳理：全文已在手（FxTwitter），失败返回空串则退回无梳理 caption
                if quote and quote.get("text"):
                    summary_src = f"{text}\n\n引用 @{quote.get('user', '')}：\n{quote['text']}"
                else:
                    summary_src = text
                summary = ""
                if summary_src.strip():
                    summary = await loop.run_in_executor(None, analyze_brief, summary_src, title)
                await _send_screenshot_album(msg, ss_paths, title, url, summary)
                return
        # 兜底：本地渲染失败才走网页截图
        for p in images:
            try: os.remove(p)
            except Exception: pass
        await _screenshot_with_summary(msg, loop, url, prefix, "", title)
        return

    # 搬运模式：合并成一段文本
    if kind == "x_quote" and quote and quote["text"]:
        user_part = f" @{quote['user']}" if quote["user"] else ""
        body_text = f"{text}\n\n———— 引用{user_part}：\n{quote['text']}" if text else f"———— 引用{user_part}：\n{quote['text']}"
    elif kind == "x_tweet":
        body_text = text
    else:  # generic
        body_text = (f"{title}\n\n{text}" if title and text else (text or title))

    full_msg = f"{body_text}\n\n🔗 {url}" if body_text else f"🔗 {url}"

    # 全空 → 截图兜底
    if not body_text and not images:
        ss_paths, _ = await loop.run_in_executor(None, webpage_screenshot, url, prefix)
        ss_paths = [p for p in ss_paths if os.path.exists(p)]
        if not ss_paths:
            await msg.reply_text(f"❌ 内容提取失败\n🔗 {url}")
            return
        ss_paths = normalize_for_telegram(ss_paths)
        cap = ((f"{title}\n\n" if title else "") + f"🔗 {url}")[:1024]
        try:
            await _send_media_with_caption(msg, ss_paths, cap)
        finally:
            for p in ss_paths:
                try: os.remove(p)
                except Exception: pass
        return

    if images:
        images = normalize_for_telegram(images)
        try:
            if len(full_msg) <= 1024:
                await _send_media_with_caption(msg, images, full_msg)
            else:
                await _send_long_text(msg, full_msg)
                await _send_media_with_caption(msg, images, f"🔗 {url}")
        finally:
            for p in images:
                try: os.remove(p)
                except Exception: pass
    else:
        await _send_long_text(msg, full_msg)


def _ensure_h264(src: str) -> str:
    """如果视频编码不是 h264/h265，转码为 h264，返回新路径；否则原路返回"""
    import subprocess as sp
    probe = sp.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", src],
        capture_output=True, text=True
    )
    codec = probe.stdout.strip().lower()
    if codec in ("h264", "h265", "hevc", ""):
        return src
    dst = src.rsplit(".", 1)[0] + "_h264.mp4"
    result = sp.run([
        "ffmpeg", "-y", "-i", src,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", dst
    ], capture_output=True)
    if result.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
        return dst
    if os.path.exists(dst):
        os.remove(dst)
    return src


def _compress_video(src: str, target_mb: float = 49.0, max_src_mb: float = 200.0) -> str:
    """用 ffmpeg 压缩视频到目标大小以内，超过 max_src_mb 的不压（会太糊）"""
    import subprocess as sp
    if os.path.getsize(src) / (1024 * 1024) > max_src_mb:
        return ""
    # 获取视频时长
    probe = sp.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", src],
        capture_output=True, text=True
    )
    duration = float(probe.stdout.strip())
    # 目标总码率(kbps)，预留音频 128k
    target_total_bitrate = int(target_mb * 8 * 1024 / duration)
    video_bitrate = max(target_total_bitrate - 128, 200)
    dst = src.rsplit(".", 1)[0] + "_compressed.mp4"
    sp.run([
        "ffmpeg", "-y", "-i", src,
        "-c:v", "libx264", "-b:v", f"{video_bitrate}k",
        "-c:a", "aac", "-b:a", "128k",
        "-preset", "fast", "-movflags", "+faststart",
        dst
    ], capture_output=True)
    if os.path.exists(dst) and os.path.getsize(dst) < target_mb * 1024 * 1024:
        return dst
    # 压缩失败或仍然太大
    if os.path.exists(dst):
        os.remove(dst)
    return ""


async def _run_subprocess(*args) -> subprocess.CompletedProcess:
    """在线程池里跑阻塞 subprocess，避免阻塞事件循环"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: subprocess.run(list(args), capture_output=True, text=True)
    )


def _get_video_dimensions(path: str):
    """用 ffprobe 读取视频宽高，返回 (w, h)，失败返回 (0, 0)"""
    import subprocess as sp
    probe = sp.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    if probe.stdout.strip():
        parts = probe.stdout.strip().split(",")
        if len(parts) == 2:
            try:
                return int(parts[0]), int(parts[1])
            except ValueError:
                pass
    return 0, 0


async def _send_video(msg, video_path: str, caption: str, clean_url: str):
    """压缩/转码后发送视频，发完清理临时文件"""
    file_size = os.path.getsize(video_path) / (1024 * 1024)
    send_path = video_path
    if file_size > 50:
        compressed = _compress_video(video_path)
        if compressed:
            send_path = compressed
        else:
            await msg.reply_text(
                f"⚠️ 视频过大（{file_size:.1f}MB），超过 200MB 不压缩，请到本地手动提取\n📁 {video_path}"
            )
            return False
    converted = _ensure_h264(send_path)
    if converted != send_path:
        if send_path != video_path and os.path.exists(send_path):
            os.remove(send_path)
        send_path = converted
    w, h = _get_video_dimensions(send_path)
    with open(send_path, "rb") as vf:
        await msg.reply_video(video=vf, width=w or None, height=h or None,
                              caption=caption[:1024], supports_streaming=True)
    if send_path != video_path and os.path.exists(send_path):
        os.remove(send_path)


async def _run_whisper(target_path: str) -> str:
    """对 target_path 跑 whisper，返回转录文本，在线程池执行不阻塞事件循环"""
    await _run_subprocess(
        "whisper", target_path, "--language", "zh", "--model", "turbo",
        "--output_format", "txt", "--output_dir", SAVE_DIR,
        "--condition_on_previous_text", "False",
        "--no_speech_threshold", "0.8",
        "--logprob_threshold", "-0.5",
        "--compression_ratio_threshold", "2.0",
    )
    txt_path = os.path.splitext(target_path)[0] + ".txt"
    if not os.path.exists(txt_path):
        return ""
    with open(txt_path) as f:
        text = f.read().strip()
    os.remove(txt_path)
    return clean_hallucination(text)


def douyin_note_gallery(url):
    """Playwright 打开抖音图文分享页抓轮播图（移动端 feed API 失效后的兜底）。
    返回 (标题, 图片URL列表)。轮播图 class 为 gallery-container__carousel__image，
    推荐流图片是 related-*/card__cover，不会混入。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 500, "height": 900},
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
        title = re.sub(r"\s*-\s*抖音$", "", page.title() or "").strip()
        srcs = page.evaluate(
            "() => Array.from(document.querySelectorAll('img.gallery-container__carousel__image'))"
            ".map(i => i.currentSrc || i.src).filter(Boolean)"
        )
        browser.close()
    seen = set()
    urls = [u for u in srcs if not (u in seen or seen.add(u))]
    return title, urls


async def _handle_xiaohongshu_note(msg, clean_url: str):
    """小红书图文帖处理"""
    await msg.reply_text("⏳ 处理中，请稍候...")
    info = None
    for _attempt in range(3):
        try:
            result = get_douyin_download_link(clean_url)
            info = json.loads(result)
            if info.get("status") != "error":
                break
            print(f"[抖音图文提取重试 {_attempt+1}/3] {info.get('error')}")
        except Exception as e:
            print(f"[抖音图文提取重试 {_attempt+1}/3] {e}")
        if _attempt < 2:
            await asyncio.sleep(2 * (_attempt + 1))
    try:
        title = ""
        images = []
        if info is not None and info.get("status") != "error":
            title = info.get("title", "")
            images = info.get("images", [])
        if not images:
            print("[图文 MCP 无图片，playwright 兜底]")
            try:
                loop = asyncio.get_event_loop()
                pw_title, images = await loop.run_in_executor(None, douyin_note_gallery, clean_url)
                title = title or pw_title
            except Exception as pw_e:
                print(f"[图文 playwright 兜底失败] {pw_e}")
        if not images:
            await msg.reply_text(f"❌ 未能提取到图片，请手动保存\n🔗 {clean_url}")
            return
        caption_text = (f"{title}\n\n" if title else "") + f"🔗 {clean_url}"
        photo_data = []
        for img_url in images:
            try:
                r = requests.get(img_url, timeout=30,
                                 headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X)"})
                if r.status_code == 200:
                    content = r.content
                    # 兜底抓到的是 webp，Telegram 图片相册要 JPEG
                    if "webp" in (r.headers.get("content-type") or "") or ".webp" in img_url:
                        from io import BytesIO
                        buf = BytesIO()
                        Image.open(BytesIO(content)).convert("RGB").save(buf, "JPEG", quality=90)
                        content = buf.getvalue()
                    photo_data.append(content)
            except Exception as img_e:
                print(f"[图片下载失败] {img_url}: {img_e}")
        if not photo_data:
            await msg.reply_text(f"❌ 未能下载到图片\n🔗 {clean_url}")
            return
        media_group = [InputMediaPhoto(media=d) for d in photo_data]
        media_group[-1] = InputMediaPhoto(media=photo_data[-1], caption=caption_text[:1024])
        await msg.reply_media_group(media=media_group)
    except Exception as e:
        print(f"[ERROR 图文] {e}")
        await msg.reply_text(f"❌ 图文提取失败：{e}\n🔗 {clean_url}")


async def _handle_qqnews(msg, clean_url: str, video_path: str) -> bool:
    """QQ新闻视频下载，成功返回 True，失败已回复消息返回 False"""
    try:
        from qq_news_extractor import download_qq_news_video
        ok = await download_qq_news_video(clean_url, video_path)
        if not ok or not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
            await _process_article(msg, clean_url)
            return False
        return True
    except Exception as e:
        print(f"[ERROR qqnews] {e}")
        import traceback; traceback.print_exc()
        await _process_article(msg, clean_url)
        return False


async def _handle_badnews(msg, clean_url: str, video_path: str) -> bool:
    """bad.news 视频下载，成功返回 True"""
    dl_url = clean_url
    if "/ajax/topic/" not in clean_url:
        m = re.search(r'/topic/(\d+)', clean_url)
        if m:
            dl_url = f"https://bad.news/ajax/topic/{m.group(1)}/download"
    cookie_dict = {}
    for part in BADNEWS_COOKIES.split(';'):
        if '=' in part:
            k, v = part.strip().split('=', 1)
            cookie_dict[k.strip()] = v.strip()
    page_hdrs = {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                 'referer': 'https://bad.news/'}
    dl_hdrs = {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        resp = requests.get(dl_url, cookies=cookie_dict, headers=page_hdrs, timeout=30)
        vid_match = re.search(r'content="\d+;\s*URL=([^"]+)"', resp.text)
        if not vid_match:
            vid_match = re.search(r'href="(https://[^"]+\.mp4[^"]*)"', resp.text)
        if not vid_match:
            await msg.reply_text("❌ 无法提取视频链接，cookies 可能已过期")
            return False
        video_dl_url = vid_match.group(1)
        r2 = requests.get(video_dl_url, headers=dl_hdrs, stream=True, timeout=60)
        if r2.status_code != 200:
            await msg.reply_text(f"❌ 视频下载失败：HTTP {r2.status_code}")
            return False
        with open(video_path, 'wb') as f:
            for chunk in r2.iter_content(chunk_size=65536):
                f.write(chunk)
        if os.path.getsize(video_path) == 0:
            os.remove(video_path)
            await msg.reply_text("❌ CDN 返回空文件，请稍后重试")
            return False
        return True
    except Exception as e:
        if os.path.exists(video_path):
            os.remove(video_path)
        await msg.reply_text(f"❌ bad.news 下载失败：{e}")
        return False


async def _handle_douyin(msg, clean_url: str, video_path: str):
    """抖音视频下载，返回 (success: bool, title: str)"""
    video_url = ""
    title = ""
    _ytdlp_fallback = False
    for _attempt in range(3):
        try:
            result = get_douyin_download_link(clean_url)
            info = json.loads(result)
            video_url = info.get("video_url") or info.get("download_url") or info.get("url", "")
            title = info.get("title") or info.get("desc", "")
            if video_url:
                break
            err_detail = info.get("error", "")
            print(f"[抖音提取重试 {_attempt+1}/3] 无视频链接 mcp={err_detail or info.get('status', '?')}")
        except Exception as e:
            print(f"[抖音提取重试 {_attempt+1}/3] {e}")
        if _attempt < 2:
            await asyncio.sleep(2 * (_attempt + 1))
        elif not video_url:
            print("[抖音 MCP 全部失败，尝试 yt-dlp 兜底]")
            douyin_cookies = os.path.expanduser("~/douyin-cookies.txt")
            douyin_cookie_args = ["--cookies", douyin_cookies] if os.path.exists(douyin_cookies) else []
            dl_fb = await _run_subprocess(
                "yt-dlp", "--no-playlist", *douyin_cookie_args, "-o", video_path, clean_url
            )
            if dl_fb.returncode == 0 and os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                _ytdlp_fallback = True
            else:
                print(f"[yt-dlp 兜底失败] {dl_fb.stderr[-200:]}")
                await _process_article(msg, clean_url)
                return False, title
    if not video_url and not _ytdlp_fallback:
        await _process_article(msg, clean_url)
        return False, title
    if not _ytdlp_fallback:
        dl = await _run_subprocess(
            "yt-dlp", "--no-playlist", "-o", video_path, video_url
        )
        if dl.returncode != 0 or not os.path.exists(video_path):
            if os.path.exists(video_path):
                os.remove(video_path)
            await msg.reply_text(f"❌ 抖音视频下载失败：{dl.stderr[-300:] if dl.stderr else 'unknown'}")
            return False, title
    if os.path.getsize(video_path) == 0:
        os.remove(video_path)
        await msg.reply_text("❌ CDN 返回空文件，请稍后重试")
        return False, title
    return True, title


async def _handle_generic_ytdlp(msg, clean_url: str, video_path: str, is_x: bool):
    """通用 yt-dlp 下载（X/YouTube/Bilibili 等），返回 (success: bool, title: str)"""
    cookies = os.path.expanduser("~/x-cookies.txt")
    cookie_args = ["--cookies", cookies] if os.path.exists(cookies) else []
    title = ""

    if is_x:
        # 标题/文案：FxTwitter 的 text 对 NoteTweet 长推即完整全文
        # （原 yt-dlp --write-info-json 取标题已随 X 提取器一起失效，删除）
        loop = asyncio.get_event_loop()
        try:
            from fx_twitter import fetch_full_text
            full = await loop.run_in_executor(None, fetch_full_text, clean_url)
            if full:
                title = full
        except Exception as e:
            print(f"[long tweet fetch failed] {e}")
        # 视频：yt-dlp 的 X 提取器被 X API 改版打挂（Could not authenticate you，
        # 2026-09 实测最新版也不行），改走 FxTwitter 的 video.twimg.com 直链（免鉴权）
        try:
            from fx_twitter import fetch_video_info, download_video
            vinfo = await loop.run_in_executor(None, fetch_video_info, clean_url)
            if vinfo:
                ok = await loop.run_in_executor(None, download_video, vinfo["url"], video_path)
                if ok:
                    return True, title or vinfo["title"]
        except Exception as e:
            print(f"[fx 视频直链失败] {e}")
        # 直链失败继续往下走 yt-dlp 兜底

    dl = await _run_subprocess(
        "yt-dlp", "--no-playlist", *cookie_args, "-o", video_path, clean_url,
    )
    if dl.returncode != 0:
        if any(h in clean_url for h in VIDEO_ONLY):
            print(f"[silent skip] {clean_url} download failed: {dl.stderr[-200:]}")
            return False, title
        try:
            await _process_article(msg, clean_url)
        except Exception as e:
            await msg.reply_text(f"❌ 下载失败：{dl.stderr[-300:]}")
        return False, title
    return True, title


async def _process(msg, clean_url: str, mode: str = "default"):
    if "/note/" in clean_url:
        await _handle_xiaohongshu_note(msg, clean_url)
        return

    await msg.reply_text("⏳ 处理中，请稍候...")

    video_path = f"{SAVE_DIR}/video_{abs(hash(clean_url))}.mp4"
    title = ""
    is_x = any(x in clean_url for x in ["twitter.com", "x.com"])
    is_douyin = any(x in clean_url for x in ["douyin.com", "v.douyin.com", "tiktok.com"])
    is_badnews = "bad.news" in clean_url
    is_qqnews = any(x in clean_url for x in ["news.qq.com", "view.inews.qq.com"])

    if is_qqnews:
        if not await _handle_qqnews(msg, clean_url, video_path):
            return
        try:
            r = requests.get(clean_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            m = re.search(r'"title":"([^"]+)"', r.text)
            if m:
                title = m.group(1)
        except Exception:
            pass

    elif is_badnews:
        if not await _handle_badnews(msg, clean_url, video_path):
            return

    elif is_douyin:
        ok, title = await _handle_douyin(msg, clean_url, video_path)
        if not ok:
            return

    else:
        ok, title = await _handle_generic_ytdlp(msg, clean_url, video_path, is_x)
        if not ok:
            return

    if not os.path.exists(video_path):
        if any(h in clean_url for h in VIDEO_ONLY):
            print(f"[silent skip] {clean_url} no video produced")
            return
        await msg.reply_text("❌ 视频下载失败")
        return

    title_prefix = f"视频标题：{title}\n\n" if title else ""
    url_suffix = f"\n\n🔗 {clean_url}"

    # 发送/转录后原视频保留在 SAVE_DIR（8/18 重构曾误改成发完即删，2026-09-01 恢复）；
    # 转码/压缩副本仍由 _send_video 内部清理
    if mode == "title_only":
        await _send_video(msg, video_path, title_prefix.rstrip() + url_suffix, clean_url)
        return

    if is_badnews:
        await _send_video(msg, video_path, "", clean_url)
        return

    if mode == "text_only":
        transcript = await _run_whisper(video_path)
        if transcript and archive_clip:
            await asyncio.to_thread(
                archive_clip, clean_url, title, transcript,
                getattr(msg, "text", "") or "", getattr(msg, "message_id", None))
        if transcript:
            full_text = title_prefix + f"文案：\n{transcript}{url_suffix}"
            while full_text:
                await msg.reply_text(full_text[:4000])
                full_text = full_text[4000:]
        else:
            await msg.reply_text(f"❌ 未能提取到文案\n🔗 {clean_url}")
        return

    # X 视频一律不转文案（2026-09-01 用户定稿：多为无意义内容，15 秒试探的
    # 连贯性判定拦不住，干脆不试探不转录，也就不会归档进 pkb）；
    # 抖音等其他平台直接全程转录
    transcript = "" if is_x else await _run_whisper(video_path)
    if transcript and archive_clip:
        await asyncio.to_thread(
            archive_clip, clean_url, title, transcript,
            getattr(msg, "text", "") or "", getattr(msg, "message_id", None))
    need_analysis = bool(transcript) and len(transcript) > 800
    analysis = analyze_transcript(transcript, title) if need_analysis else ""

    caption_with_summary = ""
    if need_analysis and analysis:
        candidate = title_prefix + f"📝 AI 梳理：\n\n{analysis}" + url_suffix
        if len(candidate) <= 1024:
            caption_with_summary = candidate
    vid_caption = caption_with_summary if caption_with_summary else (title_prefix.rstrip() + url_suffix)
    if len(vid_caption) > 1024:
        vid_caption = vid_caption[:1023] + "…"

    await _send_video(msg, video_path, vid_caption, clean_url)

    if need_analysis and analysis:
        if not caption_with_summary:
            summary_text = title_prefix + f"📝 AI 梳理：\n\n{analysis}{url_suffix}"
            while summary_text:
                await msg.reply_text(summary_text[:4000])
                summary_text = summary_text[4000:]
        full_text = title_prefix + f"原文案：\n{transcript}{url_suffix}"
        while full_text:
            await msg.reply_text(full_text[:4000])
            full_text = full_text[4000:]
    elif need_analysis and not analysis:
        await msg.reply_text("⚠️ AI 梳理失败，请检查 Ollama 是否运行")
        full_text = title_prefix + f"文案：\n{transcript}{url_suffix}"
        while full_text:
            await msg.reply_text(full_text[:4000])
            full_text = full_text[4000:]
    elif transcript:
        full_text = title_prefix + f"文案：\n{transcript}{url_suffix}"
        while full_text:
            await msg.reply_text(full_text[:4000])
            full_text = full_text[4000:]

app = ApplicationBuilder().token(BOT_TOKEN).read_timeout(300).write_timeout(600).connect_timeout(60).build()
app.add_handler(MessageHandler(filters.TEXT, handle))

# 每7天检测一次抖音解析链路是否还有效
_PROBE_VIDEO_ID = "7380153036879249699"  # 公开视频，只用来探针

async def _health_check_douyin(context: ContextTypes.DEFAULT_TYPE):
    if not BOT_OWNER:
        return
    try:
        resp = requests.get(
            "https://aweme.snssdk.com/aweme/v1/feed/",
            params={"aweme_id": _PROBE_VIDEO_ID},
            headers={"User-Agent": "com.ss.android.ugc.aweme/110101 (Linux; U; Android 10; zh_CN; Pixel 4; Build/QQ3A.200805.001; Cronet/58.0.2991.0)"},
            timeout=15,
        )
        aweme_list = resp.json().get("aweme_list", [])
        if aweme_list:
            await context.bot.send_message(chat_id=BOT_OWNER, text="✅ [周检] 抖音解析链路正常")
        else:
            await context.bot.send_message(chat_id=BOT_OWNER, text="⚠️ [周检] 抖音移动端 API 返回空列表，解析链路可能已失效，请排查")
    except Exception as e:
        await context.bot.send_message(chat_id=BOT_OWNER, text=f"❌ [周检] 抖音解析链路异常：{e}")

async def _on_startup(app):
    if not BOT_OWNER:
        return
    startup_msg = (
        "🤖 Douyin Bot 已启动\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "直接发链接 → 视频 + 文案\n"
        "加 标题 或 /title → 只发视频+标题\n"
        "加 文案 或 /text → 只发文案\n"
        "加 跳过 或 /skip → 忽略不处理\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "支持：抖音 / TikTok / X / YouTube\n"
        "      小红书 / B站 / 快手 / Instagram"
    )
    try:
        await app.bot.send_message(chat_id=BOT_OWNER, text=startup_msg)
    except Exception as e:
        print(f"[启动通知失败] {e}")

async def _on_startup_with_jobs(app):
    await _on_startup(app)
    # 每7天跑一次抖音解析健康检测，first=10s 后先跑一次确认正常
    app.job_queue.run_repeating(_health_check_douyin, interval=7 * 24 * 3600, first=10)

app.post_init = _on_startup_with_jobs
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
print("✅ Bot 启动中...")
app.run_polling(drop_pending_updates=False)
