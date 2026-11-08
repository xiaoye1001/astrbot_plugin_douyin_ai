import asyncio
import base64
import random
import re
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request

from .core import updater
from .core.douyin_client import Comment, DouyinClient, DouyinError, NotLoggedIn, Video
from .core.prompts import COMMENT_PROMPT, REPLY_PROMPT, quote
from .core.store import Store
from .core.utils import clean_output, in_active_hours, parse_json

PLUGIN_NAME = "astrbot_plugin_douyin_ai"
MANUAL_UPDATE_HINT = (
    "当前 AstrBot 版本不支持插件内一键更新，请到 AstrBot 的「插件」页面点「更新」"
)


class DouyinAIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = Store(self.data_dir / "state.json")
        self.client = DouyinClient(
            self.data_dir,
            selectors=self.config.get("selectors", {}),
            headless=self.config.get("headless", True),
            executable_path=self.config.get("browser_path", ""),
        )
        self._loop_task: asyncio.Task | None = None
        self._job_task: asyncio.Task | None = None
        self._running = False
        self._stopping = False
        self._last_browse = 0.0
        self._last_reply = 0.0
        self._last_action = 0.0
        self._last_error = ""
        self._events: deque = deque(maxlen=200)  # 网页上展示的动态
        self._login = {"status": "idle", "qr": "", "msg": ""}  # 网页扫码登录状态
        self._login_task: asyncio.Task | None = None
        self._profile_tried = 0.0
        self._update = {
            "current": updater.read_metadata().get("version", "0.0.0"),
            "latest": "",
            "has_update": False,
            "notes": "",
            "status": "idle",  # idle / checking / updating / error
            "msg": "",
            "checked_at": 0,
        }
        self._update_task: asyncio.Task | None = None
        for route, handler, methods in [
            ("state", self.web_state, ["GET"]),
            ("control", self.web_control, ["POST"]),
            ("pending/approve", self.web_approve, ["POST"]),
            ("pending/reject", self.web_reject, ["POST"]),
            ("login/start", self.web_login_start, ["POST"]),
            ("login/cookie", self.web_login_cookie, ["POST"]),
            ("login/logout", self.web_logout, ["POST"]),
            ("settings", self.web_settings, ["POST"]),
            ("update/check", self.web_update_check, ["POST"]),
            ("update/run", self.web_update_run, ["POST"]),
        ]:
            context.register_web_api(
                f"/{PLUGIN_NAME}/{route}", handler, methods, f"Douyin AI {route}"
            )
        if self.config.get("auto_run", False):
            try:
                self._start_loop()
            except RuntimeError:
                logger.warning("[douyin] 自动运行启动失败，请手动 /dy start")

    async def terminate(self):
        self._running = False
        for t in (self._loop_task, self._job_task, self._login_task):
            if t and not t.done():
                t.cancel()
        await self.client.close()

    # ================================================================ 工具方法
    def event_log(self, text: str, level: str = "info"):
        self._events.append({"t": int(time.time()), "text": text, "level": level})

    async def notify(self, text: str, image: str | None = None):
        level = "error" if text.startswith(("❌", "⚠️")) else "info"
        self.event_log(text, level)
        umo = self.store.notify_umo
        if not umo:
            logger.info(f"[douyin] {text}")
            return
        chain = MessageChain().message(text)
        if image:
            chain = chain.file_image(image)
        try:
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.warning(f"[douyin] 通知发送失败: {e}")

    async def llm(self, prompt: str) -> str:
        provider_id = self.config.get("provider_id") or ""
        if not provider_id:
            prov = await self.context.get_using_provider_async(
                self.store.notify_umo or None
            )
            if not prov:
                raise DouyinError("没有可用的大模型，请先在 AstrBot 中配置聊天模型")
            provider_id = prov.meta().id
        resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=self.config.get("persona", ""),
            prompt=prompt,
        )
        return resp.completion_text or ""

    def _finalize_text(self, text: str) -> str:
        """Admin-written text: one line + signature (no content filtering)."""
        text = re.sub(r"\s*[\r\n]+\s*", " ", text or "").strip()
        sig = self.config.get("signature", "")
        return f"{text}{sig}" if sig else text

    def _ai_text(self, decision: dict, key: str) -> str:
        """Validated AI text plus signature, or "" if it must not be posted."""
        if not decision.get(key):
            return ""
        raw = str(decision.get("text") or "")
        text = clean_output(raw)
        if raw.strip() and not text:
            self.event_log(
                f"🛡️ 已丢弃一条可疑的 AI 输出（含链接/联系方式/过长）：{raw[:60]}",
                "error",
            )
            logger.warning(f"[douyin] dropped unsafe AI output: {raw[:200]}")
            return ""
        return self._finalize_text(text) if text else ""

    async def _respect_gap(self):
        gap = int(self.config.get("min_action_gap_seconds", 90))
        wait = self._last_action + gap - time.time()
        if wait > 0:
            await asyncio.sleep(wait + random.uniform(0, 15))

    # ================================================================ AI 决策
    async def decide_comment(self, video: Video, comments: list[Comment]) -> dict:
        top = sorted(comments, key=lambda c: c.digg_count, reverse=True)[:8]
        prompt = COMMENT_PROMPT.format(
            author=quote(video.author, 40) or "未知",
            desc=quote(video.desc) or "（无文案）",
            tags=quote("、".join(video.tags), 100) or "无",
            digg=video.digg_count,
            comments="\n".join(f"- {quote(c.text, 150)}" for c in top) or "（暂无）",
        )
        return parse_json(await self.llm(prompt))

    async def decide_reply(self, desc: str, c: Comment) -> dict:
        prompt = REPLY_PROMPT.format(
            me=quote(self.client.self_nickname, 40) or "我",
            desc=quote(desc) or "（无文案）",
            user=quote(c.user, 40) or "网友",
            text=quote(c.text),
        )
        return parse_json(await self.llm(prompt))

    # ================================================================ 执行动作
    async def _do_comment(self, aweme_id: str, text: str, page=None, desc: str = ""):
        await self._respect_gap()
        await self.client.post_comment(aweme_id, text, page=page)
        self._last_action = time.time()
        self.store.mark_commented(aweme_id)
        self.store.incr_today("comments")
        self.store.add_history("comment", f"{aweme_id} {desc[:30]}", text)
        logger.info(f"[douyin] 已评论 {aweme_id}: {text}")
        self.event_log(f"✍️ 已评论「{desc[:20]}」：{text}", "success")

    async def _do_reply(self, aweme_id: str, c: Comment, text: str, page=None):
        await self._respect_gap()
        await self.client.reply_comment(aweme_id, c, text, page=page)
        self._last_action = time.time()
        self.store.mark_handled(c.cid)
        self.store.incr_today("replies")
        self.store.add_history("reply", f"{c.user}: {c.text[:30]}", text)
        logger.info(f"[douyin] 已回复 {c.user}: {text}")
        self.event_log(f"💬 已回复 {c.user}「{c.text[:20]}」：{text}", "success")

    # ================================================================ 一轮刷视频
    async def browse_round(self) -> str:
        cfg = self.config.get("browse", {})
        cap = int(cfg.get("max_comments_per_day", 10))
        review = self.config.get("review_mode", True)
        skip_kw = [k for k in cfg.get("skip_keywords", []) if k]
        watch = tuple(
            sorted(
                (
                    max(0, int(cfg.get("watch_seconds_min", 6))),
                    max(0, int(cfg.get("watch_seconds_max", 25))),
                )
            )
        )
        prob = float(cfg.get("comment_probability", 0.3))

        async with self.client.lock:
            videos = await self.client.browse_feed(
                int(cfg.get("videos_per_round", 8)),
                watch,
                should_stop=lambda: self._stopping,
            )
        made = 0
        for v in videos:
            # pending ones count too, so review mode can't pile up past the cap
            if (
                self.store.count_today("comments") + self.store.count_pending("comment")
                >= cap
            ):
                break
            if self.store.has_commented(v.aweme_id) or self.store.has_pending_for(
                "aweme_id", v.aweme_id
            ):
                continue
            if any(k in v.desc for k in skip_kw):
                continue
            if random.random() > prob:
                continue
            async with self.client.lock:
                page, detail, comments = await self.client.open_video(v.aweme_id)
                try:
                    await page.wait_for_timeout(random.randint(*watch) * 1000)
                    text = self._ai_text(
                        await self.decide_comment(detail or v, comments), "comment"
                    )
                    if not text:
                        continue
                    if review:
                        pid = self.store.add_pending(
                            {
                                "type": "comment",
                                "aweme_id": v.aweme_id,
                                "desc": v.desc[:80],
                                "author": v.author,
                                "text": text,
                            }
                        )
                        await self.notify(
                            f"📝 待审核评论 #{pid}\n视频：@{v.author} {v.desc[:60]}\n"
                            f"https://www.douyin.com/video/{v.aweme_id}\nAI 想评论：{text}\n\n"
                            f"发送 /dy ok {pid} 确认，/dy ok {pid} 新内容 修改后发送，/dy no {pid} 放弃"
                        )
                    else:
                        await self._do_comment(v.aweme_id, text, page=page, desc=v.desc)
                    made += 1
                finally:
                    await page.close()
        return f"本轮刷了 {len(videos)} 个视频，{'生成待审核评论' if review else '发表评论'} {made} 条"

    # ================================================================ 一轮回复
    async def reply_round(self) -> str:
        cfg = self.config.get("reply", {})
        cap = int(cfg.get("max_replies_per_day", 30))
        review = self.config.get("review_mode", True)
        ignore_kw = [k for k in cfg.get("ignore_keywords", []) if k]

        async with self.client.lock:
            info = await self.client.fetch_self()
        posts: list[Video] = info["posts"][: int(cfg.get("recent_videos", 5))]
        if not posts:
            return "没有获取到你的作品（账号还没发过视频，或页面加载失败）"
        made = 0

        def used():
            return self.store.count_today("replies") + self.store.count_pending("reply")

        for post in posts:
            if used() >= cap:
                break
            async with self.client.lock:
                page, _, comments = await self.client.open_video(post.aweme_id)
                try:
                    for c in comments:
                        if used() >= cap:
                            break
                        if self.store.is_handled(c.cid) or self.store.has_pending_for(
                            "cid", c.cid
                        ):
                            continue
                        if (
                            self.client.self_uid and c.user_uid == self.client.self_uid
                        ) or (
                            self.client.self_sec_uid
                            and c.user_sec_uid == self.client.self_sec_uid
                        ):
                            self.store.mark_handled(c.cid)
                            continue
                        if not c.text.strip() or any(k in c.text for k in ignore_kw):
                            self.store.mark_handled(c.cid)
                            continue
                        text = self._ai_text(
                            await self.decide_reply(post.desc, c), "reply"
                        )
                        if not text:
                            self.store.mark_handled(c.cid)
                            continue
                        if review:
                            pid = self.store.add_pending(
                                {
                                    "type": "reply",
                                    "aweme_id": post.aweme_id,
                                    "cid": c.cid,
                                    "comment": asdict(c),
                                    "text": text,
                                }
                            )
                            await self.notify(
                                f"💬 待审核回复 #{pid}\n作品：{post.desc[:40]}\n{c.user}：{c.text}\nAI 想回复：{text}\n\n"
                                f"发送 /dy ok {pid} 确认，/dy ok {pid} 新内容 修改后发送，/dy no {pid} 放弃"
                            )
                        else:
                            await self._do_reply(post.aweme_id, c, text, page=page)
                        made += 1
                finally:
                    await page.close()
        return f"检查了 {len(posts)} 个作品，{'生成待审核回复' if review else '回复'} {made} 条"

    # ================================================================ 后台循环
    def _start_loop(self):
        if self._loop_task and not self._loop_task.done():
            return
        self._running = True
        self._stopping = False
        self._last_browse = self._last_reply = 0
        self._loop_task = asyncio.get_running_loop().create_task(self._loop())

    def _stop_loop(self):
        self._running = False
        self._stopping = True
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()

    @property
    def loop_running(self) -> bool:
        return bool(self._loop_task and not self._loop_task.done())

    async def _loop(self):
        logger.info("[douyin] 自动运行已开始")
        self.event_log("▶️ 自动运行已开始")
        while self._running:
            self._maybe_auto_check()
            try:
                if in_active_hours(self.config.get("active_hours", "")):
                    now = time.time()
                    b, r = self.config.get("browse", {}), self.config.get("reply", {})
                    if (
                        r.get("enable", True)
                        and now - self._last_reply
                        >= int(r.get("interval_minutes", 30)) * 60
                    ):
                        self._last_reply = now
                        msg = await self.reply_round()
                        logger.info("[douyin] " + msg)
                        self.event_log("🔁 " + msg)
                    if (
                        b.get("enable", True)
                        and now - self._last_browse
                        >= int(b.get("interval_minutes", 60)) * 60
                    ):
                        self._last_browse = now
                        msg = await self.browse_round()
                        logger.info("[douyin] " + msg)
                        self.event_log("🎬 " + msg)
            except asyncio.CancelledError:
                break
            except NotLoggedIn as e:
                self._last_error = str(e)
                self._running = False
                await self.notify(f"⚠️ {e}，自动运行已暂停")
                break
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                self.event_log(f"运行出错：{self._last_error}", "error")
                logger.exception("[douyin] 自动运行出错")
            await asyncio.sleep(60 + random.uniform(0, 30))
        logger.info("[douyin] 自动运行已停止")
        self.event_log("⏹ 自动运行已停止")

    def _run_job(self, coro, label: str) -> bool:
        if self._job_task and not self._job_task.done():
            coro.close()
            return False
        self._stopping = False
        self.event_log(f"⏳ 开始{label}")

        async def wrapper():
            try:
                await self.notify(f"✅ {label}完成：{await coro}")
            except Exception as e:
                logger.exception(f"[douyin] {label}失败")
                await self.notify(f"❌ {label}失败：{e}")

        self._job_task = asyncio.get_running_loop().create_task(wrapper())
        return True

    # ================================================================ 审核 / 登录（指令和网页共用）
    async def approve_pending(self, pid: str, new_text: str = "") -> str:
        """发送一条待审核内容，返回实际发出的文本。失败时放回队列并抛出异常。"""
        action = self.store.pop_pending(pid)
        if not action:
            raise DouyinError(f"没有编号为 #{pid} 的待审核内容")
        text = self._finalize_text(new_text) if new_text.strip() else action["text"]
        try:
            async with self.client.lock:
                if action["type"] == "comment":
                    await self._do_comment(
                        action["aweme_id"], text, desc=action.get("desc", "")
                    )
                else:
                    await self._do_reply(
                        action["aweme_id"], Comment(**action["comment"]), text
                    )
        except Exception:
            self.store.data["pending"][str(pid)] = action  # 失败放回队列
            self.store.save()
            raise
        return text

    def reject_pending(self, pid: str) -> int:
        """放弃待审核内容，pid 为 "all" 时全部放弃。返回放弃的条数。"""
        ids = list(self.store.list_pending()) if pid == "all" else [str(pid)]
        n = 0
        for i in ids:
            a = self.store.pop_pending(i)
            if not a:
                continue
            n += 1
            if a["type"] == "reply":
                self.store.mark_handled(a["cid"])
            else:
                self.store.mark_commented(a["aweme_id"])  # 不再对这个视频生成评论
        return n

    async def login_flow(self, on_qr) -> bool:
        """扫码登录。on_qr(png_bytes) 在二维码出现时被调用。"""
        async with self.client.lock:
            ok = await self.client.login_qrcode(on_qr)
        if ok:
            try:
                await self.client.fetch_self()
            except Exception:
                pass
            self.event_log(f"🎉 抖音登录成功：{self.client.self_nickname}", "success")
        return ok

    # ================================================================ 插件更新
    async def check_update(self) -> dict:
        """Check GitHub for a newer version and remember the result."""
        self._update.update(status="checking", msg="正在检查更新…")
        try:
            info = await updater.check_latest(self._mirror())
        except Exception as e:
            self._update.update(
                status="error", msg=f"检查更新失败：{e}", checked_at=int(time.time())
            )
            raise
        self._update.update(
            info,
            status="idle",
            msg="" if info["has_update"] else "已是最新版本",
            checked_at=int(time.time()),
        )
        if (
            info["has_update"]
            and self.store.data.get("notified_version") != info["latest"]
        ):
            self.store.data["notified_version"] = info["latest"]
            self.store.save()
            await self.notify(
                f"🆕 抖音 AI 助手有新版本 v{info['latest']}（当前 v{info['current']}），"
                "可以在管理页面一键更新，或发送 /dy update"
            )
        return info

    def _maybe_auto_check(self):
        """Check for updates in the background at most every 6 hours."""
        busy = self._update_task and not self._update_task.done()
        if busy or time.time() - self._update["checked_at"] < 6 * 3600:
            return
        self._update["checked_at"] = int(time.time())  # avoid piling up retries

        async def run():
            try:
                await self.check_update()
            except Exception as e:
                logger.info(f"[douyin] 自动检查更新失败: {e}")

        self._update_task = asyncio.get_running_loop().create_task(run())

    def _mirror(self) -> str:
        m = str(self.config.get("github_mirror", updater.DOWNLOAD_MIRROR) or "").strip()
        return m if m.startswith("https://") else ""

    def _plugin_manager(self):
        """AstrBot's plugin manager, if it still offers update_plugin().

        AstrBot has no public self-update API yet; this is the same method the
        WebUI "更新" button uses. Checked up front so a future AstrBot change
        just hides the button instead of failing (tests/test_plugin.py also
        checks the signature against the real AstrBot source).
        """
        manager = getattr(self.context, "_star_manager", None)
        return manager if callable(getattr(manager, "update_plugin", None)) else None

    async def run_update(self):
        """Download the latest version and reload the plugin via AstrBot."""
        manager = self._plugin_manager()
        if manager is None:
            raise DouyinError(MANUAL_UPDATE_HINT)
        target = self._update.get("latest") or "最新版"
        self._update.update(status="updating", msg=f"正在下载 v{target}…")
        self.event_log(f"⬇️ 开始更新到 v{target}")
        mirror = self._mirror()
        try:
            try:
                await manager.update_plugin(PLUGIN_NAME)
            except Exception as e:
                if not mirror:
                    raise
                logger.warning(f"[douyin] 直接下载失败，改用加速镜像 {mirror}: {e}")
                self._update["msg"] = "直连 GitHub 失败，正在通过镜像下载…"
                await manager.update_plugin(PLUGIN_NAME, proxy=mirror)
        except TypeError as e:  # update_plugin()'s signature changed in AstrBot
            logger.warning(f"[douyin] update_plugin 调用失败: {e}")
            self._update.update(status="error", msg=MANUAL_UPDATE_HINT)
            raise DouyinError(MANUAL_UPDATE_HINT) from e
        except Exception as e:
            self._update.update(status="error", msg=f"更新失败：{e}")
            await self.notify(f"❌ 插件更新失败：{e}")
            raise
        # The plugin has been reloaded at this point; this (old) instance is retired.
        self._update.update(status="idle", msg="更新完成")
        await self.notify(f"✅ 抖音 AI 助手已更新到 v{target}")

    # ================================================================ 可视化页面 API
    async def web_state(self):
        try:
            logged = await self.client.is_logged_in()
        except Exception as e:  # e.g. playwright / chromium not installed
            logged = False
            self._last_error = str(e)
        # Fetch the nickname at most once every 10 minutes so polling stays fast
        if (
            logged
            and not self.client.self_nickname
            and not self.client.lock.locked()
            and time.time() - self._profile_tried > 600
        ):
            self._profile_tried = time.time()
            try:
                async with self.client.lock:
                    await self.client.fetch_self()
            except Exception:
                pass
        self._maybe_auto_check()
        b, r = self.config.get("browse", {}), self.config.get("reply", {})
        return json_response(
            {
                "update": {**self._update, "self_update": bool(self._plugin_manager())},
                "logged_in": logged,
                "nickname": self.client.self_nickname,
                "running": self.loop_running,
                "busy": self.client.lock.locked()
                or bool(self._job_task and not self._job_task.done()),
                "in_active_hours": in_active_hours(self.config.get("active_hours", "")),
                "last_browse": int(self._last_browse),
                "last_reply": int(self._last_reply),
                "last_error": self._last_error,
                "notify_bound": bool(self.store.notify_umo),
                "today": {
                    "comments": self.store.count_today("comments"),
                    "replies": self.store.count_today("replies"),
                    "max_comments": int(b.get("max_comments_per_day", 10)),
                    "max_replies": int(r.get("max_replies_per_day", 30)),
                },
                "pending": [
                    {"id": pid, **a} for pid, a in self.store.list_pending().items()
                ],
                "history": list(reversed(self.store.data["history"][-50:])),
                "events": list(reversed(self._events)),
                "login": self._login,
                "settings": {
                    "persona": self.config.get("persona", ""),
                    "review_mode": self.config.get("review_mode", True),
                    "signature": self.config.get("signature", ""),
                    "active_hours": self.config.get("active_hours", ""),
                    "browse_enable": b.get("enable", True),
                    "browse_interval": int(b.get("interval_minutes", 60)),
                    "max_comments_per_day": int(b.get("max_comments_per_day", 10)),
                    "comment_probability": float(b.get("comment_probability", 0.3)),
                    "reply_enable": r.get("enable", True),
                    "reply_interval": int(r.get("interval_minutes", 30)),
                    "max_replies_per_day": int(r.get("max_replies_per_day", 30)),
                },
            }
        )

    async def web_control(self):
        action = (await request.json(default={})).get("action")
        if action == "stop":
            self._stop_loop()
            return json_response({"message": "已停止自动运行"})
        if action not in ("start", "browse", "reply"):
            return error_response("未知操作", status_code=400)
        if not await self.client.is_logged_in():
            return error_response("还没登录抖音，请先扫码登录", status_code=400)
        if action == "start":
            self._start_loop()
            return json_response({"message": "已开始自动运行"})
        coro, label = (
            (self.browse_round(), "刷视频")
            if action == "browse"
            else (self.reply_round(), "检查评论")
        )
        if not self._run_job(coro, label):
            return error_response("上一个手动任务还没结束，请稍后再试", status_code=409)
        return json_response({"message": f"已开始{label}，完成后会显示在动态里"})

    async def web_approve(self):
        body = await request.json(default={})
        try:
            text = await self.approve_pending(
                str(body.get("id", "")), str(body.get("text") or "")
            )
        except Exception as e:
            msg = (str(e).splitlines() or [type(e).__name__])[0]
            return error_response(f"发送失败：{msg}", status_code=400)
        return json_response({"message": f"已发送：{text}"})

    async def web_reject(self):
        n = self.reject_pending(str((await request.json(default={})).get("id", "")))
        return json_response({"message": f"已放弃 {n} 条"})

    async def web_login_start(self):
        if await self.client.is_logged_in():
            return json_response({"message": "已经是登录状态"})
        if self._login_task and not self._login_task.done():
            return json_response({"message": "正在登录中"})
        self._login = {"status": "loading", "qr": "", "msg": "正在打开抖音登录页…"}

        def on_qr(png: bytes):
            self._login = {
                "status": "qr",
                "qr": "data:image/png;base64," + base64.b64encode(png).decode(),
                "msg": "请用抖音 App 扫码（3 分钟内有效）",
            }

        async def run():
            try:
                ok = await self.login_flow(on_qr)
                self._login = (
                    {"status": "success", "qr": "", "msg": "登录成功"}
                    if ok
                    else {"status": "timeout", "qr": "", "msg": "扫码超时，请重试"}
                )
            except Exception as e:
                self._login = {"status": "error", "qr": "", "msg": f"登录出错：{e}"}

        self._login_task = asyncio.get_running_loop().create_task(run())
        return json_response({"message": "正在获取二维码"})

    async def web_login_cookie(self):
        raw = str((await request.json(default={})).get("cookie") or "").strip()
        if "=" not in raw:
            return error_response(
                "Cookie 格式不对，应为 name=value; name2=value2", status_code=400
            )
        try:
            n = await self.client.import_cookies(raw)
            logged = await self.client.is_logged_in()
        except Exception as e:
            msg = (str(e).splitlines() or [type(e).__name__])[0]
            return error_response(f"导入失败：{msg}", status_code=400)
        if not logged:
            return error_response(
                f"导入了 {n} 个 Cookie，但没有检测到登录信息（sessionid）",
                status_code=400,
            )
        self.event_log("🍪 已通过 Cookie 登录", "success")
        return json_response({"message": f"导入成功（{n} 个 Cookie）"})

    async def web_logout(self):
        self._stop_loop()
        await self.client.logout()
        self._login = {"status": "idle", "qr": "", "msg": ""}
        self.event_log("👋 已退出抖音登录")
        return json_response({"message": "已退出登录"})

    async def web_update_check(self):
        try:
            info = await self.check_update()
        except Exception as e:
            return error_response(f"检查更新失败：{e}", status_code=502)
        msg = f"发现新版本 v{info['latest']}" if info["has_update"] else "已是最新版本"
        return json_response({"message": msg, **info})

    async def web_update_run(self):
        if self._update["status"] == "updating":
            return json_response({"message": "正在更新中"})
        if not self._update.get("has_update"):
            return error_response("已是最新版本，无需更新", status_code=400)

        async def run():
            try:
                await self.run_update()
            except Exception:
                logger.exception("[douyin] 插件更新失败")

        # Keep a reference so the task is not garbage-collected mid-update
        self._update_task = asyncio.get_running_loop().create_task(run())
        return json_response({"message": "开始更新，完成后页面会自动刷新"})

    async def web_settings(self):
        body = await request.json(default={})
        b = self.config.setdefault("browse", {})
        r = self.config.setdefault("reply", {})
        try:
            for key, target, field, cast in [
                ("persona", self.config, "persona", str),
                ("review_mode", self.config, "review_mode", bool),
                ("signature", self.config, "signature", str),
                ("active_hours", self.config, "active_hours", str),
                ("browse_enable", b, "enable", bool),
                ("browse_interval", b, "interval_minutes", int),
                ("max_comments_per_day", b, "max_comments_per_day", int),
                ("comment_probability", b, "comment_probability", float),
                ("reply_enable", r, "enable", bool),
                ("reply_interval", r, "interval_minutes", int),
                ("max_replies_per_day", r, "max_replies_per_day", int),
            ]:
                if key in body:
                    target[field] = cast(body[key])
        except (TypeError, ValueError):
            return error_response("设置的数值格式不对", status_code=400)
        hours = self.config.get("active_hours", "").strip()
        m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", hours)
        if hours and not (m and int(m[1]) <= 24 and int(m[2]) <= 24):
            return error_response(
                "活跃时间格式应为 9-23 这样（0~24 点）", status_code=400
            )
        # Keep values in sane ranges so a typo can't make the bot spam
        clamp = [
            (b, "interval_minutes", 10, 1440),
            (r, "interval_minutes", 5, 1440),
            (b, "max_comments_per_day", 0, 200),
            (r, "max_replies_per_day", 0, 500),
            (b, "comment_probability", 0.0, 1.0),
        ]
        for target, field, lo, hi in clamp:
            if field in target:
                target[field] = min(max(target[field], lo), hi)
        self.config["persona"] = str(self.config.get("persona", ""))[:2000]
        self.config["signature"] = str(self.config.get("signature", ""))[:30]
        self.config.save_config()
        self.event_log("⚙️ 设置已保存")
        return json_response({"message": "设置已保存"})

    # ================================================================ 指令
    def _bind_if_empty(self, event: AstrMessageEvent):
        if not self.store.notify_umo:
            self.store.notify_umo = event.unified_msg_origin

    @filter.command_group("dy")
    def dy(self):
        """抖音 AI 助手"""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("login")
    async def cmd_login(self, event: AstrMessageEvent):
        """扫码登录抖音"""
        self._bind_if_empty(event)
        umo = event.unified_msg_origin
        if await self.client.is_logged_in():
            yield event.plain_result(
                "已经是登录状态。如需换号，请在插件管理页面点「退出登录」。"
            )
            return
        yield event.plain_result("正在打开抖音登录页，请稍候…")

        async def on_qr(png: bytes):
            path = self.data_dir / "login_qrcode.png"
            path.write_bytes(png)
            await self.context.send_message(
                umo,
                MessageChain()
                .message("请用抖音 App 扫描二维码登录（3 分钟内有效）：")
                .file_image(str(path)),
            )

        try:
            ok = await self.login_flow(on_qr)
        except Exception as e:
            yield event.plain_result(f"登录出错：{e}")
            return
        if ok:
            name = self.client.self_nickname
            yield event.plain_result(f"🎉 登录成功{('：' + name) if name else ''}")
        else:
            yield event.plain_result(
                "扫码超时，请重新发送 /dy login。如果扫码需要短信验证，可改用 /dy cookie 导入。"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("cookie")
    async def cmd_cookie(self, event: AstrMessageEvent):
        """导入浏览器 Cookie 登录：/dy cookie <cookie字符串>"""
        self._bind_if_empty(event)
        if not event.is_private_chat():
            yield event.plain_result(
                "⚠️ Cookie 相当于你的抖音登录密码，不要在群里发！请撤回刚才的消息，"
                "改为私聊机器人发送，或在 AstrBot 管理面板的「抖音 AI 管理 → 账号」里导入。"
            )
            return
        raw = event.message_str.split("cookie", 1)[-1].strip()
        if "=" not in raw:
            yield event.plain_result(
                "用法：/dy cookie sessionid=xxx; ttwid=xxx; ...（在电脑浏览器登录抖音后，从开发者工具复制完整 Cookie）"
            )
            return
        try:
            n = await self.client.import_cookies(raw)
            ok = await self.client.is_logged_in()
        except Exception as e:
            yield event.plain_result(f"导入失败：{e}")
            return
        yield event.plain_result(
            f"已导入 {n} 个 Cookie，{'登录成功 ✅' if ok else '但未检测到 sessionid，可能无效 ❌'}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("bind")
    async def cmd_bind(self, event: AstrMessageEvent):
        """把审核/通知消息发到当前会话"""
        self.store.notify_umo = event.unified_msg_origin
        yield event.plain_result("好的，之后的审核和通知消息都会发到这里。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("start")
    async def cmd_start(self, event: AstrMessageEvent):
        """开始自动刷视频/回复评论"""
        self._bind_if_empty(event)
        if not await self.client.is_logged_in():
            yield event.plain_result("还没登录，请先 /dy login")
            return
        self._start_loop()
        mode = (
            "人工审核模式（AI 生成后需要你确认）"
            if self.config.get("review_mode", True)
            else "全自动模式"
        )
        yield event.plain_result(
            f"▶️ 已开始自动运行，当前为{mode}。活跃时间：{self.config.get('active_hours') or '全天'}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("stop")
    async def cmd_stop(self, event: AstrMessageEvent):
        """停止自动运行"""
        self._stop_loop()
        yield event.plain_result("⏹ 已停止自动运行（正在进行的操作会做完当前这一步）")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("browse")
    async def cmd_browse(self, event: AstrMessageEvent):
        """立即刷一轮视频"""
        self._bind_if_empty(event)
        if self._run_job(self.browse_round(), "刷视频"):
            yield event.plain_result("开始刷一轮视频，完成后通知你～")
        else:
            yield event.plain_result("上一个手动任务还没结束，请稍后再试")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("reply")
    async def cmd_reply(self, event: AstrMessageEvent):
        """立即检查一轮作品评论并回复"""
        self._bind_if_empty(event)
        if self._run_job(self.reply_round(), "检查评论"):
            yield event.plain_result("开始检查作品评论，完成后通知你～")
        else:
            yield event.plain_result("上一个手动任务还没结束，请稍后再试")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看运行状态"""
        logged = await self.client.is_logged_in()
        lines = [
            f"登录：{'✅ ' + (self.client.self_nickname or '') if logged else '❌ 未登录'}",
            f"自动运行：{'运行中' if self.loop_running else '已停止'}",
            f"模式：{'人工审核' if self.config.get('review_mode', True) else '全自动'}",
            f"今日评论：{self.store.count_today('comments')}/{self.config.get('browse', {}).get('max_comments_per_day', 10)}",
            f"今日回复：{self.store.count_today('replies')}/{self.config.get('reply', {}).get('max_replies_per_day', 30)}",
            f"待审核：{len(self.store.list_pending())} 条",
        ]
        if self._last_error:
            lines.append(f"最近错误：{self._last_error}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("pending")
    async def cmd_pending(self, event: AstrMessageEvent):
        """查看待审核列表"""
        p = self.store.list_pending()
        if not p:
            yield event.plain_result("没有待审核的内容")
            return
        lines = []
        for pid, a in list(p.items())[-15:]:
            if a["type"] == "comment":
                lines.append(
                    f"#{pid} 评论 @{a.get('author', '')}「{a.get('desc', '')[:20]}」→ {a['text']}"
                )
            else:
                lines.append(
                    f"#{pid} 回复 {a['comment']['user']}「{a['comment']['text'][:20]}」→ {a['text']}"
                )
        yield event.plain_result(
            "\n".join(lines)
            + "\n\n/dy ok 编号 [新内容] 发送，/dy no 编号 放弃，/dy no all 全部放弃"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("ok")
    async def cmd_ok(self, event: AstrMessageEvent):
        """确认发送待审核内容：/dy ok 编号 [修改后的内容]"""
        m = re.search(r"ok\s+(\d+)\s*(.*)$", event.message_str.strip(), re.DOTALL)
        if not m:
            yield event.plain_result("用法：/dy ok 编号 [修改后的内容]")
            return
        pid = m.group(1)
        yield event.plain_result(f"正在发送 #{pid}…")
        try:
            text = await self.approve_pending(pid, m.group(2))
        except Exception as e:
            yield event.plain_result(f"❌ 发送失败：{e}")
            return
        yield event.plain_result(f"✅ #{pid} 已发送：{text}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("no")
    async def cmd_no(self, event: AstrMessageEvent):
        """放弃待审核内容：/dy no 编号 或 /dy no all"""
        arg = event.message_str.strip().split()[-1]
        n = self.reject_pending(arg)
        if not n:
            yield event.plain_result(f"没有编号为 #{arg} 的待审核内容")
        else:
            yield event.plain_result(
                "已清空待审核列表" if arg == "all" else f"已放弃 #{arg}"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("update")
    async def cmd_update(self, event: AstrMessageEvent):
        """检查并更新插件：/dy update"""
        self._bind_if_empty(event)
        try:
            info = await self.check_update()
        except Exception as e:
            yield event.plain_result(f"检查更新失败：{e}")
            return
        if not info["has_update"]:
            yield event.plain_result(f"已是最新版本 v{info['current']} ✅")
            return
        notes = f"\n\n更新内容：\n{info['notes'][:800]}" if info["notes"] else ""
        yield event.plain_result(
            f"发现新版本 v{info['latest']}（当前 v{info['current']}），开始更新…{notes}"
        )
        try:
            await self.run_update()
        except Exception as e:
            yield event.plain_result(f"❌ 更新失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("history")
    async def cmd_history(self, event: AstrMessageEvent):
        """查看最近发出的内容"""
        h = self.store.data["history"][-15:]
        if not h:
            yield event.plain_result("还没有发过内容")
            return
        lines = [
            f"{datetime.fromtimestamp(x['t']).strftime('%m-%d %H:%M')} {'评论' if x['kind'] == 'comment' else '回复'} [{x['target']}] {x['text']}"
            for x in h
        ]
        yield event.plain_result("\n".join(lines))
