"""Browser discovery / auto-install.

Uses a local HTTP server as a stand-in for the npmmirror Chrome for Testing mirror,
serving a real chrome-headless-shell zip built from a locally installed copy.
"""

import asyncio
import json
import os
import socket
import stat
import threading
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from data.plugins.astrbot_plugin_douyin_ai.core import browser
from data.plugins.astrbot_plugin_douyin_ai.core import douyin_client as dc

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(browser.cft_platform() != "linux64", reason="linux x64 only"),
]


def find_local_headless_shell() -> Path | None:
    roots = [
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
        str(Path.home() / ".cache/ms-playwright"),
        "/opt/pw-browsers",
    ]
    for r in filter(None, roots):
        hits = sorted(
            Path(r).glob("chromium_headless_shell-*/chrome-headless-shell-linux64")
        )
        if hits:
            return hits[-1]
    return None


@pytest.fixture
def no_playwright_browser(tmp_path, monkeypatch):
    """Point Playwright at an empty browser dir so its bundled browser is 'missing'."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "empty-pw"))


@pytest.fixture
def fake_mirror(tmp_path, monkeypatch):
    src = find_local_headless_shell()
    if not src:
        pytest.skip("no local chrome-headless-shell to serve")
    version = "156.0.8078.4"
    root = tmp_path / "mirror"
    zip_dir = root / "binaries" / version / "linux64"
    zip_dir.mkdir(parents=True)
    with zipfile.ZipFile(zip_dir / "chrome-headless-shell-linux64.zip", "w") as z:
        for f in src.rglob("*"):
            if f.is_file():
                info = zipfile.ZipInfo(
                    f"chrome-headless-shell-linux64/{f.relative_to(src)}"
                )
                info.external_attr = (stat.S_IFREG | f.stat().st_mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, f.read_bytes())
    (root / "index.json").write_text(
        json.dumps([{"name": f"{version}/", "type": "dir"}])
    )

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), partial(SimpleHTTPRequestHandler, directory=str(root))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(browser, "CFT_MIRROR", f"http://127.0.0.1:{port}/binaries")
    monkeypatch.setattr(browser, "CFT_INDEX", f"http://127.0.0.1:{port}/index.json")
    monkeypatch.setattr(browser, "playwright_chromium_version", lambda: "")
    yield
    server.shutdown()


async def wait_ready(client):
    """start() raises while the background install runs; wait for it."""
    with pytest.raises(dc.DouyinError, match="正在自动下载"):
        await client.start()
    await asyncio.wait_for(client._install_task, 300)


def test_extract_zip_keeps_exec_bit_and_symlinks(tmp_path):
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        exe = zipfile.ZipInfo("d/run")
        exe.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(exe, "#!/bin/sh\n")
        link = zipfile.ZipInfo("d/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link, "run")
    browser.extract_zip(z, tmp_path / "out")
    assert os.access(tmp_path / "out/d/run", os.X_OK)
    assert (tmp_path / "out/d/link").is_symlink()


async def test_uses_system_browser_when_playwright_has_none(
    tmp_path, no_playwright_browser
):
    system = browser.system_browsers()
    if not system:
        pytest.skip("no Chrome/Chromium installed on this machine")
    c = dc.DouyinClient(tmp_path, {}, headless=True)
    try:
        await c.start()  # no download needed
        assert c._install_task is None
        assert json.loads((tmp_path / "browser.json").read_text())["path"] in system
    finally:
        await c.close()


async def test_downloads_from_mirror_when_nothing_installed(
    tmp_path, no_playwright_browser, fake_mirror, monkeypatch
):
    monkeypatch.setattr(browser, "system_browsers", lambda: [])
    monkeypatch.setattr(browser, "can_use_apt", lambda: False)
    c = dc.DouyinClient(tmp_path, {}, headless=True)
    try:
        await wait_ready(c)
        assert c._install_error == ""
        await c.start()
        exe = json.loads((tmp_path / "browser.json").read_text())["path"]
        assert exe.startswith(str(tmp_path / "browser")) and exe.endswith(
            "chrome-headless-shell"
        )
    finally:
        await c.close()

    # A fresh client (e.g. after an AstrBot restart) reuses it without downloading
    c2 = dc.DouyinClient(tmp_path, {}, headless=True)
    try:
        await c2.start()
        assert c2._install_task is None
    finally:
        await c2.close()


async def test_clear_error_when_everything_fails(
    tmp_path, no_playwright_browser, monkeypatch
):
    monkeypatch.setattr(browser, "system_browsers", lambda: [])
    monkeypatch.setattr(browser, "can_use_apt", lambda: False)
    monkeypatch.setattr(browser, "CFT_INDEX", "http://127.0.0.1:9/none")
    monkeypatch.setattr(browser, "CFT_MIRROR", "http://127.0.0.1:9/none")

    async def pw_fail(log):
        raise RuntimeError("Playwright 下载失败：network")

    monkeypatch.setattr(browser, "playwright_install", pw_fail)
    c = dc.DouyinClient(tmp_path, {}, headless=True)
    await wait_ready(c)
    with pytest.raises(dc.DouyinError) as e:
        await c.start()
    msg = str(e.value)
    assert "国内镜像" in msg and "Playwright" in msg and "Chrome 或 Edge" in msg
    # repeated polling doesn't relaunch anything for a while
    with pytest.raises(dc.DouyinError):
        await c.start()
