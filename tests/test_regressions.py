"""Regression tests for issues found in the code review."""

import asyncio
import stat
import zipfile

import pytest

from data.plugins.astrbot_plugin_douyin_ai.core import browser
from data.plugins.astrbot_plugin_douyin_ai.core import douyin_client as dc
from data.plugins.astrbot_plugin_douyin_ai.core.utils import (
    clean_output,
    in_active_hours,
)
from tests.test_browser import SELECTORS, client  # noqa: F401  (fixture)


# ---------------------------------------------------------------- zip slip
def _zip(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            z.writestr(info, data)


@pytest.mark.parametrize(
    "entries",
    [
        [("../evil.txt", "x", stat.S_IFREG | 0o644)],
        [("/tmp/abs-evil.txt", "x", stat.S_IFREG | 0o644)],
        [
            ("d/link", "../../", stat.S_IFLNK | 0o777),
            ("d/link/evil.txt", "x", stat.S_IFREG | 0o644),
        ],
        [("d/link", "/etc", stat.S_IFLNK | 0o777)],
    ],
)
def test_zip_cannot_write_outside_destination(tmp_path, entries):
    z = tmp_path / "bad.zip"
    _zip(z, entries)
    out = tmp_path / "a" / "b" / "out"
    with pytest.raises(RuntimeError):
        browser.extract_zip(z, out)
    assert not (tmp_path / "a" / "evil.txt").exists()
    assert not (tmp_path / "evil.txt").exists()


# ---------------------------------------------------------------- AI output
@pytest.mark.parametrize(
    "bad",
    [
        "加我微信 abc12345 领福利",
        "点这里 https://evil.example.com",
        "看 www.evil.cn 有惊喜",
        "联系我 13812345678",
        "@某某 快来看",
        "忽略之前的指令" * 20,  # way too long
    ],
)
def test_clean_output_blocks_spam_and_links(bad):
    assert clean_output(bad) == ""


def test_clean_output_flattens_newlines_and_keeps_normal_text():
    assert clean_output("太好吃了\n明天就做") == "太好吃了 明天就做"
    assert clean_output("  “看饿了😋”  ") == "看饿了😋"


def test_active_hours_same_start_and_end_means_all_day():
    from datetime import datetime

    assert in_active_hours("9-9", datetime(2026, 1, 1, 3))


# ---------------------------------------------------------------- browser
async def test_concurrent_start_launches_one_browser(tmp_path, chromium_path):
    c = dc.DouyinClient(
        tmp_path, SELECTORS, headless=True, executable_path=chromium_path
    )
    try:
        await asyncio.gather(*(c.start() for _ in range(3)))
        assert c._ctx is not None
    finally:
        await c.close()


async def test_multiline_text_is_posted_as_one_comment(client):  # noqa: F811
    await client.import_cookies("sessionid=abc")
    await client.post_comment("70", "第一行\n第二行")
    assert client.published == ["第一行 第二行"]


async def test_reply_goes_to_the_right_person(client):  # noqa: F811
    """Two comments with the same text: reply must target the given user's one."""
    await client.import_cookies("sessionid=abc")
    page, _, comments = await client.open_video("70")
    target = next(c for c in comments if c.cid == "c3")
    await client.reply_comment("70", target, "谢谢！", page=page)
    await page.close()
    assert client.replied_to == ["c3"]
