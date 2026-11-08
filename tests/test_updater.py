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

    async def fake_fetch(repo, path, branch="main"):
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
