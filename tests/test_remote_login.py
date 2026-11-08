"""Remote assist: the user passes a (fake) slider + SMS verification themselves
through relayed input while the plugin waits for the login to complete."""

import asyncio

import pytest

from data.plugins.astrbot_plugin_douyin_ai.core.douyin_client import (
    VIEWPORT,
    DouyinClient,
    DouyinError,
)
from tests.test_browser import SELECTORS

pytestmark = pytest.mark.browser

LOGIN_PAGE = """<html><body style="margin:0;font-family:sans-serif">
<img aria-label="二维码" style="position:absolute;left:40px;top:40px;width:120px;height:120px;background:#ddd"
     src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">
<div id=track style="position:absolute;left:100px;top:200px;width:300px;height:20px;background:#eee">
  <div id=handle style="position:absolute;left:0;top:0;width:20px;height:20px;background:#f00"></div>
</div>
<input id=sms style="position:absolute;left:100px;top:300px;width:200px;height:30px;display:none">
<script>
let down = null;
const h = document.getElementById('handle');
h.addEventListener('mousedown', e => { down = e.clientX; });
document.addEventListener('mousemove', e => { if (down !== null) h.style.left = Math.min(280, e.clientX - down) + 'px'; });
document.addEventListener('mouseup', e => {
  if (down !== null && e.clientX - down > 200) document.getElementById('sms').style.display = 'block';
  down = null;
});
document.getElementById('sms').addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.target.value === '123456')
    document.cookie = 'sessionid=passed; domain=.douyin.com; path=/';
});
</script></body></html>"""


def rel(x, y):
    return x / VIEWPORT[0], y / VIEWPORT[1]


@pytest.fixture
async def client(tmp_path, chromium_path):
    c = DouyinClient(tmp_path, SELECTORS, headless=True, executable_path=chromium_path)
    await c.start()

    async def route(r):
        await r.fulfill(body=LOGIN_PAGE, content_type="text/html; charset=utf-8")

    await c._ctx.route("https://www.douyin.com/**", route)
    yield c
    await c.close()


async def test_user_completes_verification_remotely(client):
    qr = []
    task = asyncio.create_task(client.login_qrcode(qr.append, timeout_s=60))
    for _ in range(100):
        if qr and client.login_page:
            break
        await asyncio.sleep(0.2)
    assert qr, "QR code was not captured"

    shot = await client.remote_screenshot()
    assert shot[:2] == b"\xff\xd8"  # JPEG

    # the user drags the slider handle (at x=110, y=210) to the right...
    await client.remote_drag(
        [[*rel(110, 210), 0], [*rel(200, 212), 80], [*rel(360, 210), 200]]
    )
    # ...clicks the SMS field that appeared, types the code and presses Enter
    await client.remote_click(*rel(150, 315))
    await client.remote_type("123456")
    await client.remote_key("Enter")

    assert await asyncio.wait_for(task, 30) is True
    assert client.login_page is None
    assert await client.is_logged_in()


async def test_remote_input_without_login_is_rejected(client):
    with pytest.raises(DouyinError, match="没有正在进行的登录"):
        await client.remote_click(0.5, 0.5)


async def test_only_whitelisted_keys(client):
    task = asyncio.create_task(client.login_qrcode(lambda png: None, timeout_s=30))
    for _ in range(100):
        if client.login_page:
            break
        await asyncio.sleep(0.1)
    with pytest.raises(DouyinError, match="不支持的按键"):
        await client.remote_key("Control+W")
    task.cancel()
