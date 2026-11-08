import pytest

from data.plugins.astrbot_plugin_douyin_ai.core import updater

CHANGELOG = """# 更新日志

## [0.4.0] - 2026-12-01
- 新功能 B

## [0.3.1] - 2026-11-20
- 修复 A

## [0.3.0] - 2026-11-08
- 旧内容
"""


def test_parse_version():
    assert updater.parse_version("0.3.0") == (0, 3, 0)
    assert updater.parse_version("v1.10.2") == (1, 10, 2)
    assert updater.parse_version("1.10.0") > updater.parse_version("1.9.9")
    assert updater.parse_version("") == (0,)


def test_repo_slug():
    for url in (
        "https://github.com/xiaoye1001/astrbot_plugin_douyin_ai",
        "https://github.com/xiaoye1001/astrbot_plugin_douyin_ai/",
        "https://github.com/xiaoye1001/astrbot_plugin_douyin_ai.git",
        "git@github.com:xiaoye1001/astrbot_plugin_douyin_ai.git",
    ):
        assert updater.repo_slug(url) == "xiaoye1001/astrbot_plugin_douyin_ai"
    with pytest.raises(ValueError):
        updater.repo_slug("https://gitee.com/x/y")


def test_changelog_since():
    notes = updater.changelog_since(CHANGELOG, "0.3.0")
    assert "0.4.0" in notes and "0.3.1" in notes
    assert "旧内容" not in notes
    assert updater.changelog_since(CHANGELOG, "0.4.0") == ""


def test_read_metadata_of_this_plugin():
    meta = updater.read_metadata()
    assert meta["name"] == "astrbot_plugin_douyin_ai"
    assert updater.parse_version(meta["version"]) >= (0, 3, 0)
    assert updater.repo_slug(meta["repo"])


def test_changelog_has_entry_for_current_version():
    version = updater.read_metadata()["version"]
    changelog = (updater.PLUGIN_DIR / "CHANGELOG.md").read_text("utf-8")
    assert f"## [{version}]" in changelog, "CHANGELOG.md is missing the current version"


async def test_check_latest(monkeypatch):
    remote_meta = (
        "name: astrbot_plugin_douyin_ai\nversion: 99.0.0\nastrbot_version: '>=4.29.0'\n"
    )

    async def fake_fetch(repo, path, branch="main", mirror=""):
        assert repo == "xiaoye1001/astrbot_plugin_douyin_ai"
        return remote_meta if path == "metadata.yaml" else "## [99.0.0]\n- 很新\n"

    monkeypatch.setattr(updater, "fetch_text", fake_fetch)
    info = await updater.check_latest()
    assert info["has_update"] and info["latest"] == "99.0.0"
    assert "很新" in info["notes"]

    remote_meta = remote_meta.replace("99.0.0", "0.0.1")
    info = await updater.check_latest()
    assert not info["has_update"] and info["notes"] == ""


async def test_fetch_text_falls_back_to_mirrors(monkeypatch):
    import httpx

    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "raw.githubusercontent.com":
            raise httpx.ConnectError("blocked")
        return httpx.Response(200, text="version: 1.0.0")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        updater.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    assert await updater.fetch_text("a/b", "metadata.yaml") == "version: 1.0.0"
    assert calls == ["raw.githubusercontent.com", "ghfast.top"]


# ---------------------------------------------------------------- AstrBot OpenAPI
@pytest.fixture
def clean_env(monkeypatch):
    for k in (
        "DASHBOARD_PORT", "ASTRBOT_DASHBOARD_PORT", "DASHBOARD_HOST",
        "ASTRBOT_DASHBOARD_HOST", "DASHBOARD_SSL_ENABLE", "ASTRBOT_DASHBOARD_SSL_ENABLE",
    ):  # fmt: skip
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_dashboard_base_url(clean_env):
    assert updater.dashboard_base_url({}) == ("http://127.0.0.1:6185", True)
    assert updater.dashboard_base_url({"host": "0.0.0.0", "port": 8080}) == (
        "http://127.0.0.1:8080",
        True,
    )
    assert updater.dashboard_base_url(
        {"host": "10.0.0.5", "port": 6185, "ssl": {"enable": True}}
    ) == ("https://10.0.0.5:6185", False)
    assert updater.dashboard_base_url({"host": "::1"})[0] == "http://[::1]:6185"
    clean_env.setenv("ASTRBOT_DASHBOARD_PORT", "7000")
    clean_env.setenv("DASHBOARD_SSL_ENABLE", "true")
    assert updater.dashboard_base_url({"port": 6185}) == (
        "https://127.0.0.1:7000",
        True,
    )


_REAL_CLIENT = __import__("httpx").AsyncClient


def _mock_api(monkeypatch, handler):
    import httpx

    real = _REAL_CLIENT
    monkeypatch.setattr(
        updater.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


async def test_update_via_openapi(monkeypatch):
    import httpx

    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"status": "ok", "message": "更新成功。"})

    _mock_api(monkeypatch, handler)
    await updater.update_via_openapi(
        "http://127.0.0.1:6185",
        "abk_test",
        "astrbot_plugin_douyin_ai",
        "https://ghfast.top",
    )
    assert (
        seen["url"]
        == "http://127.0.0.1:6185/api/v1/plugins/astrbot_plugin_douyin_ai/update"
    )
    assert seen["auth"] == "Bearer abk_test"
    assert b"ghfast" in seen["body"]


async def test_update_via_openapi_errors(monkeypatch):
    import httpx

    _mock_api(monkeypatch, lambda r: httpx.Response(403, json={"status": "error"}))
    with pytest.raises(PermissionError, match="plugin 权限"):
        await updater.update_via_openapi("http://x", "bad", "p")

    _mock_api(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": "error", "message": "下载失败"}),
    )
    with pytest.raises(RuntimeError, match="下载失败"):
        await updater.update_via_openapi("http://x", "k", "p")


async def test_update_refused_when_install_source_unknown(monkeypatch):
    import httpx

    _mock_api(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"status": "error", "message": "请先选择插件安装源后再更新。"}
        ),
    )
    with pytest.raises(updater.UpdateRefused, match="选择安装源"):
        await updater.update_via_openapi("http://x", "k", "p")
