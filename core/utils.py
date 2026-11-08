"""Small helpers shared by the plugin."""

import json
import re
from datetime import datetime


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
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
