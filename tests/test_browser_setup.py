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
import time
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


class RangeHandler(SimpleHTTPRequestHandler):
    """Static files with HTTP Range support and an optional per-connection
    speed limit (like a CDN that throttles each connection)."""

    ranges = True
    bytes_per_sec = 0
    seen_ranges: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            return self.send_error(404)
        data = path.read_bytes()
        rng = self.headers.get("Range") if self.ranges else None
        if rng:
            lo, hi = rng.split("=")[1].split("-")
            lo, hi = int(lo), min(int(hi or len(data) - 1), len(data) - 1)
            type(self).seen_ranges.append((lo, hi))
            body = data[lo : hi + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {lo}-{hi}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        step = 64 << 10
        for i in range(0, len(body), step):
            try:
                self.wfile.write(body[i : i + step])
            except (BrokenPipeError, ConnectionResetError):
                return
            if self.bytes_per_sec:
                time.sleep(step / self.bytes_per_sec)


def serve(root: Path, handler=RangeHandler):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), partial(handler, directory=str(root))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


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

    server, base = serve(root)
    monkeypatch.setattr(browser, "CFT_MIRROR", f"{base}/binaries")
    monkeypatch.setattr(browser, "CFT_INDEX", f"{base}/index.json")
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


# ---------------------------------------------------------------- speed-ups
@pytest.fixture
def blob(tmp_path):
    data = os.urandom(12 << 20)  # 12 MB: big enough for parallel parts
    (tmp_path / "srv").mkdir()
    (tmp_path / "srv" / "f.bin").write_bytes(data)
    return data


async def _download(url, path, **kw):
    import httpx

    logs = []
    async with httpx.AsyncClient(timeout=30) as client:
        t = time.monotonic()
        await browser.fast_download(client, url, path, logs.append, "下载", **kw)
        return time.monotonic() - t, logs


async def test_parallel_download_is_correct_and_faster(tmp_path, blob, monkeypatch):
    class Throttled(RangeHandler):
        bytes_per_sec = 6 << 20  # 6 MB/s per connection
        seen_ranges = []

    server, base = serve(tmp_path / "srv", Throttled)
    try:
        t_one, _ = await _download(f"{base}/f.bin", tmp_path / "one.bin", parts=1)
        t_many, logs = await _download(f"{base}/f.bin", tmp_path / "many.bin", parts=8)
    finally:
        server.shutdown()
    assert (tmp_path / "one.bin").read_bytes() == blob
    assert (tmp_path / "many.bin").read_bytes() == blob
    assert len([r for r in Throttled.seen_ranges if r != (0, 0)]) == 8
    assert t_many < t_one / 2, (t_one, t_many)
    assert "100%" in logs[-1] and "MB/s" in logs[-1]


async def test_download_falls_back_when_range_unsupported(tmp_path, blob):
    class NoRange(RangeHandler):
        ranges = False

    server, base = serve(tmp_path / "srv", NoRange)
    try:
        await _download(f"{base}/f.bin", tmp_path / "out.bin")
    finally:
        server.shutdown()
    assert (tmp_path / "out.bin").read_bytes() == blob


async def test_download_404(tmp_path, blob):
    server, base = serve(tmp_path / "srv")
    try:
        with pytest.raises(browser._NotFound):
            await _download(f"{base}/missing.bin", tmp_path / "x.bin")
    finally:
        server.shutdown()


async def test_fastest_mirror_picks_quickest(tmp_path):
    release = "dists/bookworm/Release"
    for name in ("slow", "fast"):
        (tmp_path / name / "dists/bookworm").mkdir(parents=True)
        (tmp_path / name / release).write_bytes(os.urandom(300 << 10))

    class Slow(RangeHandler):
        bytes_per_sec = 200 << 10

    s1, slow = serve(tmp_path / "slow", Slow)
    s2, fast = serve(tmp_path / "fast")
    try:
        mirrors = [
            ("慢", slow, slow),
            ("挂了", "http://127.0.0.1:9", "http://127.0.0.1:9"),
            ("快", fast, fast),
        ]
        best = await browser.fastest_mirror("bookworm", mirrors)
    finally:
        s1.shutdown()
        s2.shutdown()
    assert best[0] == "快"


async def test_apt_uses_temporary_sources_list(tmp_path, monkeypatch):
    calls = []

    async def fake_run(*cmd, env=None, timeout=900):
        calls.append(cmd)
        return 0, ""

    async def fake_fastest(codename, mirrors=None):
        return (
            "阿里云",
            "https://mirrors.aliyun.com/debian",
            "https://mirrors.aliyun.com/debian-security",
        )

    monkeypatch.setattr(browser, "_run", fake_run)
    monkeypatch.setattr(browser, "debian_codename", lambda: "bookworm")
    monkeypatch.setattr(browser, "fastest_mirror", fake_fastest)
    await browser.apt_install_fast(["chromium"], tmp_path / "apt", lambda m: None)

    sources = (tmp_path / "apt" / "sources.list").read_text()
    assert "deb https://mirrors.aliyun.com/debian bookworm main" in sources
    assert (
        "deb https://mirrors.aliyun.com/debian-security bookworm-security main"
        in sources
    )
    update, install = calls
    assert f"Dir::Etc::SourceList={tmp_path / 'apt' / 'sources.list'}" in update
    assert "update" in update and "--error-on=any" in update
    assert install[-1] == "chromium"
    assert not any("/etc/apt" in part for part in update + install)
