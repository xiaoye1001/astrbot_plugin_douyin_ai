"""抖音网页端自动化（基于 Playwright）。

设计思路：
- 使用持久化浏览器目录保存登录态，扫码登录一次即可长期使用。
- 视频、评论等数据通过「监听网页自己发出的接口响应」获取，而不是解析 DOM，
  这样网页改版时影响更小。
- 发评论 / 回复只能操作 DOM，选择器可在插件配置里修改；
  是否发送成功以抖音的 comment/publish 接口返回结果为准。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

DOUYIN_HOME = "https://www.douyin.com/"
VIDEO_URL = "https://www.douyin.com/video/{aweme_id}"
SELF_URL = "https://www.douyin.com/user/self"

FEED_API_KEYS = (
    "/aweme/v1/web/tab/feed",
    "/aweme/v2/web/module/feed",
    "/aweme/v1/web/feed",
)
COMMENT_LIST_API = "/aweme/v1/web/comment/list/"
REPLY_LIST_API = "/aweme/v1/web/comment/list/reply/"
PUBLISH_API = "/aweme/v1/web/comment/publish"
SELF_PROFILE_API = "/aweme/v1/web/user/profile/self"
POST_LIST_API = "/aweme/v1/web/aweme/post/"


class DouyinError(Exception):
    pass


class NotLoggedIn(DouyinError):
    pass


@dataclass
class Video:
    aweme_id: str
    desc: str = ""
    author: str = ""
    author_sec_uid: str = ""
    duration_s: float = 0
    digg_count: int = 0
    comment_count: int = 0
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, d: dict) -> Video | None:
        aid = str(d.get("aweme_id") or "")
        if not aid:
            return None
        author = d.get("author") or {}
        stats = d.get("statistics") or {}
        video = d.get("video") or {}
        tags = [
            t.get("hashtag_name", "")
            for t in (d.get("text_extra") or [])
            if isinstance(t, dict) and t.get("hashtag_name")
        ]
        return cls(
            aweme_id=aid,
            desc=d.get("desc") or "",
            author=author.get("nickname") or "",
            author_sec_uid=author.get("sec_uid") or "",
            duration_s=(video.get("duration") or 0) / 1000,
            digg_count=int(stats.get("digg_count") or 0),
            comment_count=int(stats.get("comment_count") or 0),
            tags=tags,
        )


@dataclass
class Comment:
    cid: str
    text: str
    user: str = ""
    user_uid: str = ""
    user_sec_uid: str = ""
    create_time: int = 0
    digg_count: int = 0
    reply_total: int = 0
    reply_to_cid: str = ""  # 楼中楼时为所属的一级评论

    @classmethod
    def from_json(cls, d: dict) -> Comment | None:
        cid = str(d.get("cid") or "")
        if not cid:
            return None
        user = d.get("user") or {}
        return cls(
            cid=cid,
            text=d.get("text") or "",
            user=user.get("nickname") or "",
            user_uid=str(user.get("uid") or ""),
            user_sec_uid=user.get("sec_uid") or "",
            create_time=int(d.get("create_time") or 0),
            digg_count=int(d.get("digg_count") or 0),
            reply_total=int(d.get("reply_comment_total") or 0),
            reply_to_cid=str(d.get("reply_id") or "")
            if str(d.get("reply_id") or "0") != "0"
            else "",
        )


def _iter_awemes(obj: Any):
    """在任意 JSON 结构里找出视频对象（兼容不同版本的 feed 接口）。"""
    if isinstance(obj, dict):
        if "aweme_id" in obj and ("desc" in obj or "author" in obj):
            yield obj
            return
        for k, v in obj.items():
            # module/feed 接口里 aweme 是一段 JSON 字符串
            if k == "aweme" and isinstance(v, str):
                try:
                    yield from _iter_awemes(json.loads(v))
                except Exception:
                    pass
            else:
                yield from _iter_awemes(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _iter_awemes(it)


def split_selectors(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split("||") if x.strip()]


class DouyinClient:
    def __init__(
        self,
        data_dir: Path,
        selectors: dict,
        headless: bool = True,
        executable_path: str = "",
    ):
        self.executable_path = executable_path
        self.data_dir = Path(data_dir)
        self.profile_dir = self.data_dir / "browser_profile"
        self.selectors = selectors or {}
        self.headless = headless
        self._pw = None
        self._ctx = None
        self._install_task: asyncio.Task | None = None
        self._install_error = ""
        self.lock = asyncio.Lock()  # 同一时间只做一件事
        self.self_uid: str = ""
        self.self_sec_uid: str = ""
        self.self_nickname: str = ""

    # ------------------------------------------------------------------ 生命周期
    async def start(self):
        if self._ctx:
            return
        if self._install_task and not self._install_task.done():
            raise DouyinError(
                "正在自动下载浏览器（第一次使用需要 1~5 分钟），请稍后再试"
            )
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise DouyinError(
                "未安装 playwright，请在 AstrBot 所在环境执行: pip install playwright"
            ) from e
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                viewport={"width": 1366, "height": 860},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                **(
                    {"executable_path": self.executable_path}
                    if self.executable_path
                    else {}
                ),
            )
        except Exception as e:
            await self._pw.stop()
            self._pw = None
            msg = str(e)
            missing_browser = "Executable doesn't exist" in msg
            missing_deps = (
                "missing dependencies" in msg
                or "error while loading shared libraries" in msg
            )
            if self.executable_path or not (missing_browser or missing_deps):
                raise
            if self._install_task is None:
                # First failure: download the browser in the background
                self._install_task = asyncio.get_running_loop().create_task(
                    self._install_browser(with_deps=missing_deps)
                )
                raise DouyinError(
                    "第一次使用，正在自动下载浏览器（约 1~5 分钟），请稍后再试"
                ) from e
            raise DouyinError(
                f"自动安装浏览器失败：{self._install_error or msg.splitlines()[0]}。"
                "请在 AstrBot 所在环境手动执行: python -m playwright install --with-deps chromium，然后重载插件"
            ) from e
        logger.info("[douyin] 浏览器已启动")

    async def _install_browser(self, with_deps: bool):
        """Run `playwright install chromium`, falling back to a China mirror."""

        async def run(*args: str, env: dict | None = None) -> tuple[int, str]:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "playwright",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, **(env or {})},
            )
            out, _ = await proc.communicate()
            return proc.returncode, out.decode(errors="ignore")[-500:]

        logger.info("[douyin] 正在下载 Chromium 浏览器…")
        if with_deps:
            code, out = await run("install-deps", "chromium")
            if code != 0:
                logger.warning(f"[douyin] 安装系统依赖失败: {out}")
        code, out = await run("install", "chromium")
        if code != 0:
            logger.warning(f"[douyin] 官方源下载失败，改用国内镜像: {out}")
            code, out = await run(
                "install",
                "chromium",
                env={
                    "PLAYWRIGHT_DOWNLOAD_HOST": "https://npmmirror.com/mirrors/playwright"
                },
            )
        if code == 0:
            logger.info("[douyin] 浏览器下载完成")
            self._install_task = (
                None  # allow a fresh launch (and another try if needed)
            )
            self._install_error = ""
        else:
            # Pick the most useful line, e.g. "Error: Failed to download ..."
            lines = [x.strip() for x in out.splitlines() if x.strip()]
            errs = [x for x in lines if "Error" in x and not x.startswith("at ")]
            self._install_error = (errs or lines or ["未知错误"])[0][:200]
            logger.error(f"[douyin] 浏览器下载失败: {out}")

    async def close(self):
        try:
            if self._ctx:
                await self._ctx.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self._ctx = None
            self._pw = None

    async def _new_page(self):
        await self.start()
        return await self._ctx.new_page()

    def _sel(self, key: str) -> list[str]:
        return split_selectors(self.selectors.get(key, ""))

    async def _find(self, scope, key: str, timeout: float = 4000, visible=True):
        """按配置里的多个候选选择器依次查找，返回第一个找到的元素。"""
        for sel in self._sel(key):
            loc = scope.locator(sel).first
            try:
                await loc.wait_for(
                    state="visible" if visible else "attached", timeout=timeout
                )
                return loc
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------ 登录
    async def is_logged_in(self) -> bool:
        await self.start()
        cookies = await self._ctx.cookies("https://www.douyin.com")
        return any(
            c["name"] in ("sessionid", "sessionid_ss") and c["value"] for c in cookies
        )

    async def import_cookies(self, cookie_str: str) -> int:
        """导入从浏览器复制的 Cookie 字符串（name=value; name2=value2）。"""
        await self.start()
        items = []
        for part in cookie_str.split(";"):
            if "=" not in part:
                continue
            name, value = part.strip().split("=", 1)
            if name:
                items.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": ".douyin.com",
                        "path": "/",
                        # Give imported cookies an expiry so they survive restarts
                        "expires": time.time() + 60 * 86400,
                    }
                )
        if items:
            await self._ctx.add_cookies(items)
        return len(items)

    async def login_qrcode(
        self, on_qrcode: Callable[[bytes], Any], timeout_s: int = 180
    ) -> bool:
        """打开登录弹窗，把二维码截图交给 on_qrcode 回调，然后等待扫码完成。"""
        page = await self._new_page()
        try:
            await page.goto(DOUYIN_HOME, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)
            if await self.is_logged_in():
                return True
            qr = await self._find(page, "qrcode", timeout=3000)
            if not qr:
                btn = await self._find(page, "login_button", timeout=5000)
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(2500)
                qr = await self._find(page, "qrcode", timeout=8000)
            png = await (qr.screenshot() if qr else page.screenshot())
            r = on_qrcode(png)
            if asyncio.iscoroutine(r):
                await r
            for _ in range(timeout_s // 3):
                await page.wait_for_timeout(3000)
                if await self.is_logged_in():
                    await page.wait_for_timeout(3000)
                    return True
            return False
        finally:
            await page.close()

    async def ensure_login(self):
        if not await self.is_logged_in():
            raise NotLoggedIn("抖音未登录，请先发送 /dy login 扫码登录")

    async def fetch_self(self) -> dict:
        """获取自己的账号信息和最近作品。"""
        await self.ensure_login()
        page = await self._new_page()
        profile: dict = {}
        posts: list[Video] = []

        async def on_resp(resp):
            try:
                if SELF_PROFILE_API in resp.url:
                    profile.update((await resp.json()).get("user") or {})
                elif POST_LIST_API in resp.url:
                    for a in (await resp.json()).get("aweme_list") or []:
                        v = Video.from_json(a)
                        if v:
                            posts.append(v)
            except Exception:
                pass

        page.on("response", on_resp)
        try:
            await page.goto(SELF_URL, wait_until="domcontentloaded")
            for _ in range(10):
                await page.wait_for_timeout(1500)
                if profile and posts:
                    break
        finally:
            await page.close()
        if profile:
            self.self_uid = str(profile.get("uid") or "")
            self.self_sec_uid = profile.get("sec_uid") or ""
            self.self_nickname = profile.get("nickname") or ""
        return {"profile": profile, "posts": posts}

    # ------------------------------------------------------------------ 刷视频
    async def browse_feed(
        self, count: int, watch_range: tuple[int, int], should_stop=None
    ) -> list[Video]:
        """在推荐页往下刷 count 个视频，返回刷到的视频信息。"""
        await self.ensure_login()
        page = await self._new_page()
        seen: dict[str, Video] = {}

        async def on_resp(resp):
            if not any(k in resp.url for k in FEED_API_KEYS):
                return
            try:
                data = await resp.json()
            except Exception:
                return
            for a in _iter_awemes(data):
                v = Video.from_json(a)
                if v and v.aweme_id not in seen:
                    seen[v.aweme_id] = v

        page.on("response", on_resp)
        try:
            await page.goto(DOUYIN_HOME + "?recommend=1", wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            # 关掉可能出现的弹窗，之后用方向键切换视频
            await page.keyboard.press("Escape")
            for _ in range(count):
                if should_stop and should_stop():
                    break
                lo, hi = watch_range
                await page.wait_for_timeout(int(random.uniform(lo, max(lo, hi)) * 1000))
                await page.keyboard.press("ArrowDown")
                await page.wait_for_timeout(random.randint(800, 1800))
        finally:
            await page.close()
        return list(seen.values())[: max(count, 1) + 2]

    # ------------------------------------------------------------------ 视频页
    async def open_video(self, aweme_id: str):
        """打开单个视频页，返回 (page, video, comments)。调用方负责关闭 page。"""
        await self.ensure_login()
        page = await self._new_page()
        info: dict = {}
        comments: list[Comment] = []

        async def on_resp(resp):
            try:
                if COMMENT_LIST_API in resp.url and REPLY_LIST_API not in resp.url:
                    for c in (await resp.json()).get("comments") or []:
                        cm = Comment.from_json(c)
                        if cm:
                            comments.append(cm)
                elif "/aweme/v1/web/aweme/detail" in resp.url:
                    info.update((await resp.json()).get("aweme_detail") or {})
            except Exception:
                pass

        page.on("response", on_resp)
        try:
            await page.goto(
                VIDEO_URL.format(aweme_id=aweme_id), wait_until="domcontentloaded"
            )
            for _ in range(8):
                await page.wait_for_timeout(1200)
                if comments:
                    break
            if not comments:
                icon = await self._find(page, "comment_icon", timeout=1500)
                if icon:
                    await icon.click()
                    await page.wait_for_timeout(3000)
        except Exception:
            await page.close()  # do not leak the tab when the video fails to load
            raise
        video = Video.from_json(info) if info else None
        return page, video, comments

    async def _type_and_send(self, page, scope, text: str) -> dict:
        editor = await self._find(scope, "editor", timeout=4000)
        if not editor:
            editor = await self._find(page, "editor", timeout=2000)
        if not editor:
            raise DouyinError(
                "找不到评论输入框，网页可能已改版，请在插件配置的「页面选择器」中调整"
            )
        await editor.click()
        await page.wait_for_timeout(random.randint(300, 900))
        async with page.expect_response(
            lambda r: PUBLISH_API in r.url, timeout=15000
        ) as resp_info:
            await page.keyboard.type(text, delay=random.randint(60, 160))
            await page.wait_for_timeout(random.randint(500, 1200))
            await page.keyboard.press("Enter")
        resp = await resp_info.value
        try:
            data = await resp.json()
        except Exception:
            data = {}
        if data.get("status_code", -1) != 0:
            raise DouyinError(
                f"抖音拒绝了这条评论: {data.get('status_msg') or data or resp.status}"
            )
        return data.get("comment") or {}

    async def post_comment(self, aweme_id: str, text: str, page=None) -> dict:
        own_page = page is None
        if own_page:
            page, _, _ = await self.open_video(aweme_id)
        try:
            entry = await self._find(page, "comment_entry", timeout=4000)
            if entry:
                await entry.click()
                await page.wait_for_timeout(800)
            return await self._type_and_send(page, page, text)
        finally:
            if own_page:
                await page.close()

    async def reply_comment(
        self, aweme_id: str, comment: Comment, text: str, page=None
    ) -> dict:
        own_page = page is None
        if own_page:
            page, _, _ = await self.open_video(aweme_id)
        try:
            item = await self._locate_comment(page, comment)
            if not item:
                raise DouyinError(
                    "在页面上没找到要回复的评论（可能需要往下翻或已被删除）"
                )
            await item.hover()
            await page.wait_for_timeout(400)
            btn = None
            for sel in self._sel("reply_button"):
                loc = item.locator(sel).first
                if await loc.count():
                    btn = loc
                    break
            if not btn:
                raise DouyinError(
                    "找不到「回复」按钮，请在插件配置的「页面选择器」中调整"
                )
            await btn.click()
            await page.wait_for_timeout(800)
            return await self._type_and_send(page, item, text)
        finally:
            if own_page:
                await page.close()

    async def _locate_comment(self, page, comment: Comment):
        snippet = comment.text.strip()[:20]
        if not snippet:
            return None
        for _ in range(6):
            for sel in self._sel("comment_item"):
                loc = page.locator(sel).filter(has_text=snippet)
                if await loc.count():
                    return loc.first
            # 滚动评论区加载更多
            await page.mouse.wheel(0, 1200)
            await page.wait_for_timeout(1500)
        return None

    async def logout(self):
        """清除登录态（换号时使用）。"""
        await self.start()
        await self._ctx.clear_cookies()
        self.self_uid = self.self_sec_uid = self.self_nickname = ""
