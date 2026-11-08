"""Find or install a Chromium-based browser for Playwright.

Downloading Playwright's own browser often fails from mainland China, so the
plugin tries, in order:

1. a browser already on the machine (Chrome / Edge / Chromium) - no download
2. Chrome for Testing from the npmmirror CDN (fast in China)
3. the system package manager (``apt-get install chromium``) on Linux as root
4. ``playwright install chromium`` (official CDN, then npmmirror)
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import stat
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx

# Chrome for Testing mirror (same files as storage.googleapis.com/chrome-for-testing-public)
CFT_MIRROR = os.environ.get(
    "DOUYIN_CFT_MIRROR", "https://cdn.npmmirror.com/binaries/chrome-for-testing"
)
CFT_INDEX = os.environ.get(
    "DOUYIN_CFT_INDEX", "https://registry.npmmirror.com/-/binary/chrome-for-testing/"
)
PLAYWRIGHT_MIRROR = "https://npmmirror.com/mirrors/playwright"

Log = Callable[[str], None]


def system_browsers() -> list[str]:
    """Chrome / Edge / Chromium executables installed on this machine."""
    found: list[str] = []
    if sys.platform == "win32":
        roots = [
            os.environ.get(k)
            for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        ]
        rels = [
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"Chromium\Application\chrome.exe",
        ]
        found += [str(Path(r) / rel) for r in roots if r for rel in rels]
    elif sys.platform == "darwin":
        for app, exe in [
            ("Google Chrome", "Google Chrome"),
            ("Microsoft Edge", "Microsoft Edge"),
            ("Chromium", "Chromium"),
        ]:
            for base in ("/Applications", str(Path.home() / "Applications")):
                found.append(f"{base}/{app}.app/Contents/MacOS/{exe}")
    else:
        for name in (
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
            "microsoft-edge",
            "microsoft-edge-stable",
        ):
            if p := shutil.which(name):
                found.append(p)
    seen, result = set(), []
    for p in found:
        if p not in seen and Path(p).is_file():
            seen.add(p)
            result.append(p)
    return result


def cft_platform() -> str | None:
    """Platform name used by Chrome for Testing downloads, or None if unsupported."""
    machine = platform.machine().lower()
    if sys.platform == "win32":
        return "win64" if machine in ("amd64", "x86_64") else None
    if sys.platform == "darwin":
        return "mac-arm64" if machine in ("arm64", "aarch64") else "mac-x64"
    if sys.platform.startswith("linux"):
        return "linux64" if machine in ("x86_64", "amd64") else None  # no arm64 builds
    return None


def cft_executable(root: Path, plat: str, headless_shell: bool) -> Path:
    """Path of the browser binary inside an extracted Chrome for Testing zip."""
    if headless_shell:
        exe = (
            "chrome-headless-shell.exe" if plat == "win64" else "chrome-headless-shell"
        )
        return root / f"chrome-headless-shell-{plat}" / exe
    if plat == "win64":
        return root / "chrome-win64" / "chrome.exe"
    if plat.startswith("mac"):
        return (
            root
            / f"chrome-{plat}"
            / "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        )
    return root / f"chrome-{plat}" / "chrome"


def playwright_chromium_version() -> str:
    """Chromium version the installed Playwright was built against (best match)."""
    try:
        import playwright

        f = Path(playwright.__file__).parent / "driver/package/browsers.json"
        for b in json.loads(f.read_text("utf-8"))["browsers"]:
            if b["name"] == "chromium":
                return b.get("browserVersion", "")
    except Exception:
        pass
    return ""


def _version_key(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip("/").split("."))
    except ValueError:
        return (0,)


def extract_zip(zip_path: Path, dest: Path):
    """Extract keeping executable bits and symlinks (zipfile drops both)."""
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = dest / info.filename
            mode = info.external_attr >> 16
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                link = z.read(info).decode()
                if target.is_symlink() or target.exists():
                    target.unlink()
                os.symlink(link, target)
                continue
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            if mode and os.name != "nt":
                os.chmod(target, stat.S_IMODE(mode) | stat.S_IRUSR)


async def download_cft(dest: Path, headless_shell: bool, log: Log) -> str:
    """Download Chrome for Testing from the mirror. Returns the executable path."""
    plat = cft_platform()
    if not plat:
        raise RuntimeError(
            f"国内镜像没有适用于 {sys.platform}/{platform.machine()} 的浏览器"
        )
    kind = "chrome-headless-shell" if headless_shell else "chrome"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        versions = [v for v in [playwright_chromium_version()] if v]
        try:
            resp = await client.get(CFT_INDEX)
            listed = [
                x["name"].strip("/") for x in resp.json() if x.get("type") == "dir"
            ]
            versions += sorted(listed, key=_version_key, reverse=True)[:8]
        except Exception as e:
            log(f"读取镜像版本列表失败（{type(e).__name__}），尝试默认版本")
        last_err = "镜像上没有可用的版本"
        for ver in dict.fromkeys(versions):
            url = f"{CFT_MIRROR}/{ver}/{plat}/{kind}-{plat}.zip"
            zip_path = dest / f"{kind}-{plat}-{ver}.zip"
            dest.mkdir(parents=True, exist_ok=True)
            try:
                async with client.stream("GET", url, timeout=None) as r:
                    if r.status_code != 200:
                        last_err = f"{ver}: HTTP {r.status_code}"
                        continue
                    total = int(r.headers.get("content-length") or 0)
                    done, shown = 0, -1
                    with open(zip_path, "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 20):
                            f.write(chunk)
                            done += len(chunk)
                            pct = done * 100 // total if total else -1
                            if pct // 10 != shown // 10:
                                shown = pct
                                log(
                                    f"正在从国内镜像下载浏览器 {ver}… {pct}%"
                                    if pct >= 0
                                    else "正在从国内镜像下载浏览器…"
                                )
                root = dest / f"cft-{ver}"
                shutil.rmtree(root, ignore_errors=True)
                await asyncio.to_thread(extract_zip, zip_path, root)
                exe = cft_executable(root, plat, headless_shell)
                if not exe.is_file():
                    raise RuntimeError(f"压缩包里没有找到 {exe.name}")
                return str(exe)
            except (httpx.HTTPError, OSError, zipfile.BadZipFile) as e:
                last_err = f"{ver}: {type(e).__name__} {e}"
            finally:
                zip_path.unlink(missing_ok=True)
    raise RuntimeError(f"国内镜像下载失败（{last_err}）")


async def _run(
    *cmd: str, env: dict | None = None, timeout: int = 900
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **(env or {})},
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "超时"
    text = out.decode(errors="ignore")
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    errs = [
        x for x in lines if ("rror" in x or "失败" in x) and not x.startswith("at ")
    ]
    return proc.returncode or 0, (errs or lines or [""])[-1][:200]


def can_use_apt() -> bool:
    return (
        sys.platform.startswith("linux")
        and shutil.which("apt-get") is not None
        and hasattr(os, "geteuid")
        and os.geteuid() == 0
    )


async def apt_install(packages: list[str], log: Log) -> None:
    env = {"DEBIAN_FRONTEND": "noninteractive"}
    log("正在更新系统软件源…")
    await _run("apt-get", "update", env=env)
    log(f"正在用系统软件源安装 {' '.join(packages)}…")
    code, out = await _run(
        "apt-get", "install", "-y", "--no-install-recommends", *packages, env=env
    )
    if code != 0:
        raise RuntimeError(f"apt-get install 失败：{out}")


async def install_playwright_deps(log: Log) -> None:
    """Install the shared libraries Chromium needs (Linux, root, apt)."""
    log("正在安装浏览器需要的系统组件…")
    code, out = await _run(
        sys.executable, "-m", "playwright", "install-deps", "chromium"
    )
    if code != 0:
        raise RuntimeError(f"安装系统组件失败：{out}")


async def playwright_install(log: Log) -> None:
    """`playwright install chromium`, official CDN first, then npmmirror."""
    log("正在通过 Playwright 官方源下载浏览器…")
    code, out = await _run(sys.executable, "-m", "playwright", "install", "chromium")
    if code == 0:
        return
    log("官方源下载失败，改用 Playwright 国内镜像…")
    code, out2 = await _run(
        sys.executable,
        "-m",
        "playwright",
        "install",
        "chromium",
        env={"PLAYWRIGHT_DOWNLOAD_HOST": PLAYWRIGHT_MIRROR},
    )
    if code != 0:
        raise RuntimeError(f"Playwright 下载失败：{out2 or out}")
