"""Drive the real browser automation against a local fake of douyin.com.

The fake page serves the same API paths and DOM hooks the plugin relies on, so this
catches regressions in feed parsing, comment posting and replying.
"""

import json

import pytest

from data.plugins.astrbot_plugin_douyin_ai.core.douyin_client import DouyinClient
from data.plugins.astrbot_plugin_douyin_ai.core.updater import PLUGIN_DIR

pytestmark = pytest.mark.browser

SELECTORS = {
    k: v["default"]
    for k, v in json.loads((PLUGIN_DIR / "_conf_schema.json").read_text("utf-8"))[
        "selectors"
    ]["items"].items()
}

AWEMES = [
    {
        "aweme_id": f"7{i}",
        "desc": f"视频{i} 今天做了红烧肉 #美食",
        "author": {"nickname": f"作者{i}"},
        "statistics": {"digg_count": 100 + i},
        "video": {"duration": 15000},
        "text_extra": [{"hashtag_name": "美食"}],
    }
    for i in range(5)
]
COMMENTS = [
    {"cid": "c1", "text": "看起来好好吃啊", "user": {"nickname": "小王", "uid": "u1"}},
    {
        "cid": "c2",
        "text": "教程能再详细点吗",
        "user": {"nickname": "小李", "uid": "u2"},
    },
]

PAGE = """<html><body><div id=feed>推荐</div>
<div class="comment-input-inner-container">留下你的精彩评论吧</div>
<div id=list></div>
<script>
fetch('/aweme/v2/web/module/feed/?x=1').then(r=>r.json());
fetch('/aweme/v1/web/comment/list/?aweme_id=1').then(r=>r.json()).then(d=>{
  for (const c of d.comments) {
    const el=document.createElement('div'); el.setAttribute('data-e2e','comment-item');
    el.innerHTML='<span>'+c.text+'</span> <span class=rb>回复</span>';
    el.querySelector('.rb').onclick=()=>{const e=document.createElement('div');e.contentEditable='true';el.appendChild(e);};
    document.getElementById('list').appendChild(el);
  }});
document.querySelector('.comment-input-inner-container').onclick=()=>{
  const e=document.createElement('div');e.contentEditable='true';e.className='public-DraftEditor-content';document.body.appendChild(e);};
document.addEventListener('keydown',ev=>{ if(ev.key==='Enter'){
  fetch('/aweme/v1/web/comment/publish',{method:'POST',body:document.activeElement.innerText}); }});
</script></body></html>"""


@pytest.fixture
async def client(tmp_path, chromium_path):
    published = []

    async def route(r):
        url = r.request.url
        if "module/feed" in url:
            body = {
                "cards": [{"aweme": json.dumps(a, ensure_ascii=False)} for a in AWEMES]
            }
        elif "comment/list" in url:
            body = {"comments": COMMENTS}
        elif "comment/publish" in url:
            published.append(r.request.post_data)
            body = {"status_code": 0, "comment": {"cid": "new"}}
        else:
            return await r.fulfill(body=PAGE, content_type="text/html; charset=utf-8")
        await r.fulfill(
            body=json.dumps(body, ensure_ascii=False), content_type="application/json"
        )

    c = DouyinClient(tmp_path, SELECTORS, headless=True, executable_path=chromium_path)
    await c.start()
    await c._ctx.route("https://www.douyin.com/**", route)
    c.published = published
    yield c
    await c.close()


async def test_cookie_login_detection(client):
    assert not await client.is_logged_in()
    assert await client.import_cookies("sessionid=abc; ttwid=1") == 2
    assert await client.is_logged_in()
    await client.logout()
    assert not await client.is_logged_in()


async def test_feed_comment_and_reply(client):
    await client.import_cookies("sessionid=abc")
    videos = await client.browse_feed(3, (0, 0))
    assert [v.aweme_id for v in videos][:3] == ["70", "71", "72"]
    assert videos[0].author == "作者0" and videos[0].tags == ["美食"]

    page, _, comments = await client.open_video("70")
    assert [c.cid for c in comments] == ["c1", "c2"]
    await client.post_comment("70", "看饿了", page=page)
    await client.reply_comment("70", comments[1], "下期安排！", page=page)
    await page.close()
    assert client.published == ["看饿了", "下期安排！"]
