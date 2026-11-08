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


MAX_OUTPUT_LEN = 100

# Things a normal short comment never needs. Comments and video captions are written
# by strangers and go into the prompt, so a malicious one could try to make the AI
# post spam / contact info / links from the user's account ("prompt injection").
_BLOCKED = [
    re.compile(r"https?://|www\.", re.I),
    re.compile(
        r"[a-z0-9-]+\.(com|cn|net|org|top|xyz|cc|vip|io|me|link|site|shop)\b", re.I
    ),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),  # mobile number
    re.compile(r"\d{6,}"),  # QQ / group numbers and other long ids
    re.compile(r"@\S"),  # mentioning other users
    re.compile(
        r"微信|v信|vx|wx|qq|加我|私信|联系方式|群号|兼职|返利|领福利|代理", re.I
    ),
]


def clean_output(text: str) -> str:
    """Normalise AI-written text; return "" if it must not be posted.

    Flattens newlines (Enter would submit the comment early), strips wrapping
    quotes, and rejects anything too long or containing links / contact info.
    """
    text = re.sub(r"\s*[\r\n]+\s*", " ", text or "").strip().strip("\"“”'「」")
    text = re.sub(r"\s{2,}", " ", text).strip()
    if not text or len(text) > MAX_OUTPUT_LEN:
        return ""
    if any(p.search(text) for p in _BLOCKED):
        return ""
    return text


def in_active_hours(spec: str, now: datetime | None = None) -> bool:
    spec = (spec or "").strip()
    if not spec:
        return True
    try:
        a, b = (int(x) for x in spec.split("-", 1))
    except ValueError:
        return True
    if a == b:
        return True  # e.g. "0-0": all day
    h = (now or datetime.now()).hour
    return a <= h < b if a < b else (h >= a or h < b)
