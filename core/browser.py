"""Find or install a Chromium-based browser for Playwright.

Downloading Playwright's own browser often fails from mainland China, so the
plugin tries, in order:

1. a browser already on the machine (Chrome / Edge / Chromium) - no download
2. Chrome for Testing from the npmmirror CDN (fast in China)
3. the system package manager (``apt-get install chromium``) on Linux as root,
   using the fastest of several domestic Debian mirrors
4. ``playwright install chromium`` (official CDN, then npmmirror)
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import stat
import sys
import time
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


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def extract_zip(zip_path: Path, dest: Path):
    """Extract keeping executable bits and symlinks (zipfile drops both).

    Every entry must stay inside ``dest``: "../", absolute paths and symlinks that
    point (or lead) outside are rejected, so a tampered download can't overwrite
    other files on the machine.
    """
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                raise RuntimeError(f"压缩包里有不安全的路径：{info.filename}")
            target = dest / name
            mode = info.external_attr >> 16
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                if not _inside(target.resolve(), root):
                    raise RuntimeError(f"压缩包里有不安全的路径：{info.filename}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # resolve() follows symlinks created by earlier entries
            if not _inside(target.parent.resolve(), root):
                raise RuntimeError(f"压缩包里有不安全的路径：{info.filename}")
            if stat.S_ISLNK(mode):
                link = z.read(info).decode()
                if Path(link).is_absolute() or not _inside(
                    (target.parent / link).resolve(), root
                ):
                    raise RuntimeError(
                        f"压缩包里有不安全的链接：{info.filename} -> {link}"
                    )
                if target.is_symlink() or target.exists():
                    target.unlink()
                os.symlink(link, target)
                continue
            if target.is_symlink():
                target.unlink()  # never write through a link
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            if mode and os.name != "nt":
                os.chmod(target, stat.S_IMODE(mode) | stat.S_IRUSR)


class _NotFound(Exception):
    pass


def _fmt_size(n: float) -> str:
    return f"{n / 1048576:.1f} MB"


async def fast_download(
    client: httpx.AsyncClient,
    url: str,
    path: Path,
    log: Log,
    label: str,
    parts: int = 8,
) -> None:
    """Download ``url`` to ``path`` using several parallel Range requests.

    CDNs often throttle each connection, so N connections are usually much
    faster than one. Falls back to a single stream if Range isn't supported.
    Raises _NotFound on HTTP 404.
    """
    # Ask for 1 byte to learn the size; stream so a server that ignores Range
    # doesn't push the whole file into memory here.
    async with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as head:
        status, headers = head.status_code, head.headers
        if status == 404:
            raise _NotFound(url)
        if status not in (200, 206):
            raise httpx.HTTPStatusError(
                f"HTTP {status}", request=head.request, response=head
            )
    total = 0
    if status == 206:
        m = re.search(r"/(\d+)$", headers.get("content-range", ""))
        total = int(m.group(1)) if m else 0

    done = 0
    started = time.monotonic()
    last_report = 0.0

    def report(final: bool = False):
        nonlocal last_report
        now = time.monotonic()
        if not final and now - last_report < 1.5:
            return
        last_report = now
        speed = done / max(now - started, 0.001)
        if total:
            log(
                f"{label} {done * 100 // total}%（{_fmt_size(done)} / {_fmt_size(total)}，"
                f"{_fmt_size(speed)}/s）"
            )
        else:
            log(f"{label} {_fmt_size(done)}（{_fmt_size(speed)}/s）")

    async def fetch(start: int | None, end: int | None, f):
        nonlocal done
        headers = {"Range": f"bytes={start}-{end}"} if start is not None else {}
        for attempt in range(3):  # retry a dropped part, resuming where it stopped
            try:
                async with client.stream(
                    "GET", url, headers=headers, timeout=None
                ) as r:
                    if r.status_code not in (200, 206):
                        raise httpx.HTTPStatusError(
                            f"HTTP {r.status_code}", request=r.request, response=r
                        )
                    async for chunk in r.aiter_bytes(1 << 18):
                        f.write(chunk)
                        done += len(chunk)
                        if start is not None:
                            start += len(chunk)
                            headers["Range"] = f"bytes={start}-{end}"
                        report()
                return
            except httpx.HTTPError:
                if attempt == 2 or start is None:
                    raise
                await asyncio.sleep(1 + attempt)

    path.parent.mkdir(parents=True, exist_ok=True)
    if total < 8 << 20 or parts <= 1:
        with open(path, "wb") as f:
            await fetch(None, None, f)
    else:
        with open(path, "wb") as f:
            f.truncate(total)
        size = -(-total // parts)
        files = [open(path, "r+b") for _ in range(parts)]  # noqa: SIM115
        try:
            jobs = []
            for i, f in enumerate(files):
                lo, hi = i * size, min(total, (i + 1) * size) - 1
                f.seek(lo)
                jobs.append(fetch(lo, hi, f))
            await asyncio.gather(*jobs)
        finally:
            for f in files:
                f.close()
        if done != total:
            raise httpx.HTTPError(f"下载不完整（{done}/{total} 字节）")
    report(final=True)


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
                try:
                    await fast_download(
                        client, url, zip_path, log, f"正在从国内镜像下载浏览器 {ver}"
                    )
                except _NotFound:
                    last_err = f"{ver}: HTTP 404"
                    continue
                log("下载完成，正在解压…")
                root = dest / f"cft-{ver}"
                shutil.rmtree(root, ignore_errors=True)
                await asyncio.to_thread(extract_zip, zip_path, root)
                exe = cft_executable(root, plat, headless_shell)
                if not exe.is_file():
                    raise RuntimeError(f"压缩包里没有找到 {exe.name}")
                return str(exe)
            except (httpx.HTTPError, OSError, zipfile.BadZipFile, RuntimeError) as e:
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


# (name, debian, debian-security). Probed for speed; only used for this one install.
DEBIAN_MIRRORS = [
    (
        "阿里云",
        "https://mirrors.aliyun.com/debian",
        "https://mirrors.aliyun.com/debian-security",
    ),
    (
        "清华",
        "https://mirrors.tuna.tsinghua.edu.cn/debian",
        "https://mirrors.tuna.tsinghua.edu.cn/debian-security",
    ),
    (
        "中科大",
        "https://mirrors.ustc.edu.cn/debian",
        "https://mirrors.ustc.edu.cn/debian-security",
    ),
    (
        "华为云",
        "https://repo.huaweicloud.com/debian",
        "https://repo.huaweicloud.com/debian-security",
    ),
    (
        "Debian 官方",
        "https://deb.debian.org/debian",
        "https://security.debian.org/debian-security",
    ),
]


def debian_codename() -> str:
    """'bookworm' / 'trixie' on Debian (AstrBot's Docker image), else ''."""
    try:
        text = Path("/etc/os-release").read_text("utf-8")
    except OSError:
        return ""
    info = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    clean = {k: v.strip().strip('"') for k, v in info.items()}
    return clean.get("VERSION_CODENAME", "") if clean.get("ID") == "debian" else ""


async def fastest_mirror(codename: str, mirrors=None) -> tuple[str, str, str] | None:
    """Download each mirror's small Release file at once; return the quickest."""
    mirrors = mirrors or DEBIAN_MIRRORS

    async def probe(client, m):
        t = time.monotonic()
        r = await client.get(f"{m[1]}/dists/{codename}/Release")
        r.raise_for_status()
        return time.monotonic() - t, m

    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
        results = await asyncio.gather(
            *(probe(client, m) for m in mirrors), return_exceptions=True
        )
    ok = sorted(r for r in results if isinstance(r, tuple))
    return ok[0][1] if ok else None


async def apt_install_fast(packages: list[str], workdir: Path, log: Log) -> None:
    """apt-get install using the fastest Debian mirror, without touching
    /etc/apt (a temporary sources list is passed on the command line)."""
    codename = debian_codename()
    if not codename:
        raise RuntimeError("不是 Debian 系统")
    log("正在测速国内软件源…")
    best = await fastest_mirror(codename)
    if not best:
        raise RuntimeError("所有软件源都连不上")
    name, main, security = best
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "parts").mkdir(exist_ok=True)
    sources = workdir / "sources.list"
    sources.write_text(
        f"deb {main} {codename} main\n"
        f"deb {main} {codename}-updates main\n"
        f"deb {security} {codename}-security main\n",
        "utf-8",
    )
    opts = [
        "-o", f"Dir::Etc::SourceList={sources}",
        "-o", f"Dir::Etc::SourceParts={workdir / 'parts'}",
        "-o", "Acquire::Retries=3",
    ]  # fmt: skip
    env = {"DEBIAN_FRONTEND": "noninteractive"}
    log(f"正在从{name}软件源更新列表…")
    # --error-on=any: fail here if the mirror is unreachable (apt ≥ 2.1, Debian 11+)
    code, out = await _run("apt-get", *opts, "update", "--error-on=any", env=env)
    if code != 0:
        raise RuntimeError(f"{name}软件源更新失败：{out}")
    log(f"正在从{name}软件源安装 {' '.join(packages)}（含所需系统组件）…")
    code, out = await _run(
        "apt-get", *opts, "install", "-y", "--no-install-recommends", *packages, env=env
    )
    if code != 0:
        raise RuntimeError(f"{name}软件源安装失败：{out}")


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
