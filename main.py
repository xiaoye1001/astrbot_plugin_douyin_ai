import asyncio
import json
import random
import re
import time
from dataclasses import asdict
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .douyin_client import Comment, DouyinClient, DouyinError, NotLoggedIn, Video
from .store import Store

PLUGIN_NAME = "astrbot_plugin_douyin_ai"

COMMENT_PROMPT = """你正在刷抖音，刷到了下面这个视频。请以你的人设决定要不要评论，以及评论什么。

【视频作者】{author}
【视频文案】{desc}
【话题】{tags}
【点赞数】{digg}
【已有热门评论】
{comments}

要求：
1. 评论要和视频内容相关，口语化、简短，像真人随手评论；不要复读热门评论。
2. 如果视频内容看不懂、信息太少、是广告/引战/敏感话题，就不要评论。
3. 不要编造自己的经历，不要留联系方式、链接或引流内容。
只输出一个 JSON：{{"comment": true 或 false, "text": "评论内容", "reason": "一句话理由"}}"""

REPLY_PROMPT = """你是抖音博主「{me}」，下面是你的作品下面的一条新评论，请决定是否回复以及回复内容。

【你的作品文案】{desc}
【评论者】{user}
【评论内容】{text}

要求：
1. 回复要友好、自然、简短，像博主本人在回复粉丝。
2. 如果评论是辱骂、广告、无意义内容（如纯表情、"1"），或你不确定怎么回答的事实性问题，就不要回复。
3. 不要承诺任何你无法确认的事情（价格、发货、合作等）。
只输出一个 JSON：{{"reply": true 或 false, "text": "回复内容", "reason": "一句话理由"}}"""


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def in_active_hours(spec: str, now: datetime | None = None) -> bool:
    spec = (spec or "").strip()
    if not spec:
        return True
    try:
        a, b = (int(x) for x in spec.split("-", 1))
    except ValueError:
        return True
    h = (now or datetime.now()).hour
    return a <= h < b if a <= b else (h >= a or h < b)


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
        if self.config.get("auto_run", False):
            try:
                self._start_loop()
            except RuntimeError:
                logger.warning("[douyin] 自动运行启动失败，请手动 /dy start")

    async def terminate(self):
        self._running = False
        for t in (self._loop_task, self._job_task):
            if t and not t.done():
                t.cancel()
        await self.client.close()

    # ================================================================ 工具方法
    async def notify(self, text: str, image: str | None = None):
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
        text = (text or "").strip().strip('"“”')
        sig = self.config.get("signature", "")
        return f"{text}{sig}" if sig else text

    async def _respect_gap(self):
        gap = int(self.config.get("min_action_gap_seconds", 90))
        wait = self._last_action + gap - time.time()
        if wait > 0:
            await asyncio.sleep(wait + random.uniform(0, 15))

    # ================================================================ AI 决策
    async def decide_comment(self, video: Video, comments: list[Comment]) -> dict:
        top = sorted(comments, key=lambda c: c.digg_count, reverse=True)[:8]
        prompt = COMMENT_PROMPT.format(
            author=video.author or "未知",
            desc=video.desc or "（无文案）",
            tags="、".join(video.tags) or "无",
            digg=video.digg_count,
            comments="\n".join(f"- {c.text}" for c in top) or "（暂无）",
        )
        return parse_json(await self.llm(prompt))

    async def decide_reply(self, desc: str, c: Comment) -> dict:
        prompt = REPLY_PROMPT.format(
            me=self.client.self_nickname or "我",
            desc=desc or "（无文案）",
            user=c.user or "网友",
            text=c.text,
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

    async def _do_reply(self, aweme_id: str, c: Comment, text: str, page=None):
        await self._respect_gap()
        await self.client.reply_comment(aweme_id, c, text, page=page)
        self._last_action = time.time()
        self.store.mark_handled(c.cid)
        self.store.incr_today("replies")
        self.store.add_history("reply", f"{c.user}: {c.text[:30]}", text)
        logger.info(f"[douyin] 已回复 {c.user}: {text}")

    # ================================================================ 一轮刷视频
    async def browse_round(self) -> str:
        cfg = self.config.get("browse", {})
        cap = int(cfg.get("max_comments_per_day", 10))
        review = self.config.get("review_mode", True)
        skip_kw = [k for k in cfg.get("skip_keywords", []) if k]
        watch = (
            int(cfg.get("watch_seconds_min", 6)),
            int(cfg.get("watch_seconds_max", 25)),
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
            if self.store.count_today("comments") >= cap:
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
                    d = await self.decide_comment(detail or v, comments)
                    if not d.get("comment") or not d.get("text"):
                        continue
                    text = self._finalize_text(d["text"])
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
        for post in posts:
            if self.store.count_today("replies") >= cap:
                break
            async with self.client.lock:
                page, _, comments = await self.client.open_video(post.aweme_id)
                try:
                    for c in comments:
                        if self.store.count_today("replies") >= cap:
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
                        d = await self.decide_reply(post.desc, c)
                        if not d.get("reply") or not d.get("text"):
                            self.store.mark_handled(c.cid)
                            continue
                        text = self._finalize_text(d["text"])
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
        self._loop_task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self):
        logger.info("[douyin] 自动运行已开始")
        while self._running:
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
                        logger.info("[douyin] " + await self.reply_round())
                    if (
                        b.get("enable", True)
                        and now - self._last_browse
                        >= int(b.get("interval_minutes", 60)) * 60
                    ):
                        self._last_browse = now
                        logger.info("[douyin] " + await self.browse_round())
            except asyncio.CancelledError:
                break
            except NotLoggedIn as e:
                self._last_error = str(e)
                self._running = False
                await self.notify(f"⚠️ {e}，自动运行已暂停")
                break
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                logger.exception("[douyin] 自动运行出错")
            await asyncio.sleep(60 + random.uniform(0, 30))
        logger.info("[douyin] 自动运行已停止")

    def _run_job(self, coro, label: str) -> bool:
        if self._job_task and not self._job_task.done():
            coro.close()
            return False
        self._stopping = False

        async def wrapper():
            try:
                await self.notify(f"✅ {label}完成：{await coro}")
            except Exception as e:
                logger.exception(f"[douyin] {label}失败")
                await self.notify(f"❌ {label}失败：{e}")

        self._job_task = asyncio.get_running_loop().create_task(wrapper())
        return True

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
                "已经是登录状态。如需换号，请删除插件数据目录下的 browser_profile 后重试。"
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
            async with self.client.lock:
                ok = await self.client.login_qrcode(on_qr)
        except Exception as e:
            yield event.plain_result(f"登录出错：{e}")
            return
        if ok:
            try:
                info = await self.client.fetch_self()
                name = info["profile"].get("nickname", "")
            except Exception:
                name = ""
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
        raw = event.message_str.split("cookie", 1)[-1].strip()
        if "=" not in raw:
            yield event.plain_result(
                "用法：/dy cookie sessionid=xxx; ttwid=xxx; ...（在电脑浏览器登录抖音后，从开发者工具复制完整 Cookie）"
            )
            return
        n = await self.client.import_cookies(raw)
        ok = await self.client.is_logged_in()
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
        self._last_browse = self._last_reply = 0
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
        self._running = False
        self._stopping = True
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
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
        running = bool(self._loop_task and not self._loop_task.done())
        lines = [
            f"登录：{'✅ ' + (self.client.self_nickname or '') if logged else '❌ 未登录'}",
            f"自动运行：{'运行中' if running else '已停止'}",
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
        m = re.search(r"ok\s+(\d+)\s*(.*)$", event.message_str.strip(), re.S)
        if not m:
            yield event.plain_result("用法：/dy ok 编号 [修改后的内容]")
            return
        pid, new_text = m.group(1), m.group(2).strip()
        action = self.store.pop_pending(pid)
        if not action:
            yield event.plain_result(f"没有编号为 #{pid} 的待审核内容")
            return
        text = self._finalize_text(new_text) if new_text else action["text"]
        yield event.plain_result(f"正在发送 #{pid}…")
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
        except Exception as e:
            self.store.data["pending"][pid] = action  # 失败放回队列
            self.store.save()
            yield event.plain_result(f"❌ 发送失败：{e}\n内容已放回待审核列表")
            return
        yield event.plain_result(f"✅ #{pid} 已发送：{text}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @dy.command("no")
    async def cmd_no(self, event: AstrMessageEvent):
        """放弃待审核内容：/dy no 编号 或 /dy no all"""
        arg = event.message_str.strip().split()[-1]
        if arg == "all":
            for pid in list(self.store.list_pending()):
                a = self.store.pop_pending(pid)
                if a and a["type"] == "reply":
                    self.store.mark_handled(a["cid"])
            yield event.plain_result("已清空待审核列表")
            return
        a = self.store.pop_pending(arg)
        if not a:
            yield event.plain_result(f"没有编号为 #{arg} 的待审核内容")
            return
        if a["type"] == "reply":
            self.store.mark_handled(a["cid"])
        else:
            self.store.mark_commented(a["aweme_id"])  # 不再对这个视频生成评论
        yield event.plain_result(f"已放弃 #{arg}")

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
