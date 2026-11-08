"""插件的本地状态：待审核队列、已处理记录、每日计数。保存为 JSON 文件。"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path


class Store:
    MAX_IDS = 5000

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = {
            "notify_umo": "",
            "commented_awemes": [],  # 评论过的视频
            "handled_cids": [],  # 已处理（回复或跳过）的评论
            "pending": {},  # 编号 -> 待审核动作
            "next_pending_id": 1,
            "daily": {},  # 日期 -> {"comments": n, "replies": n}
            "history": [],  # 最近发出的内容
        }
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text("utf-8")))
            except Exception:
                pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for k in ("commented_awemes", "handled_cids"):
            self.data[k] = self.data[k][-self.MAX_IDS :]
        self.data["history"] = self.data["history"][-200:]
        today = date.today().isoformat()
        self.data["daily"] = {
            k: v for k, v in self.data["daily"].items() if k >= _days_ago(7)
        } or {today: {}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(self.path)

    # ---- 通知会话
    @property
    def notify_umo(self) -> str:
        return self.data.get("notify_umo") or ""

    @notify_umo.setter
    def notify_umo(self, v: str):
        self.data["notify_umo"] = v
        self.save()

    # ---- 去重
    def has_commented(self, aweme_id: str) -> bool:
        return aweme_id in self.data["commented_awemes"]

    def mark_commented(self, aweme_id: str):
        if aweme_id not in self.data["commented_awemes"]:
            self.data["commented_awemes"].append(aweme_id)
        self.save()

    def is_handled(self, cid: str) -> bool:
        return cid in self.data["handled_cids"]

    def mark_handled(self, cid: str):
        if cid not in self.data["handled_cids"]:
            self.data["handled_cids"].append(cid)
        self.save()

    # ---- 每日计数
    def count_today(self, kind: str) -> int:
        return self.data["daily"].get(date.today().isoformat(), {}).get(kind, 0)

    def incr_today(self, kind: str):
        d = self.data["daily"].setdefault(date.today().isoformat(), {})
        d[kind] = d.get(kind, 0) + 1
        self.save()

    def add_history(self, kind: str, target: str, text: str):
        self.data["history"].append(
            {"t": int(time.time()), "kind": kind, "target": target, "text": text}
        )
        self.save()

    # ---- 待审核
    def add_pending(self, action: dict) -> int:
        pid = self.data["next_pending_id"]
        self.data["next_pending_id"] = pid + 1
        action["created"] = int(time.time())
        self.data["pending"][str(pid)] = action
        self.save()
        return pid

    def pop_pending(self, pid) -> dict | None:
        a = self.data["pending"].pop(str(pid), None)
        self.save()
        return a

    def list_pending(self) -> dict:
        return self.data["pending"]

    def has_pending_for(self, key: str, value: str) -> bool:
        return any(a.get(key) == value for a in self.data["pending"].values())


def _days_ago(n: int) -> str:
    from datetime import timedelta

    return (date.today() - timedelta(days=n)).isoformat()
