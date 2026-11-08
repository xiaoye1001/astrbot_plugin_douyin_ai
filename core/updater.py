"""Check GitHub for a newer plugin version.

The actual download / replace / reload is done by AstrBot's own plugin updater
(the same one behind the "更新" button on the WebUI plugin page).
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx

PLUGIN_DIR = Path(__file__).resolve().parent.parent

# Tried in order. raw.githubusercontent.com is often unreachable from mainland
# China, so fall back to public mirrors.
RAW_URL = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
CDN_URL = "https://fastly.jsdelivr.net/gh/{repo}@{branch}/{path}"

# Default URL-prefix mirror (configurable as `github_mirror`); "" disables it.
DOWNLOAD_MIRROR = "https://ghfast.top"


def read_metadata(text: str | None = None) -> dict[str, str]:
    """Read simple ``key: value`` pairs from metadata.yaml (no YAML dependency)."""
    if text is None:
        text = (PLUGIN_DIR / "metadata.yaml").read_text("utf-8")
    meta = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_]+):\s*(.*?)\s*$", line)
        if m:
            meta[m.group(1)] = m.group(2).strip("\"'")
    return meta


def parse_version(v: str) -> tuple[int, ...]:
    """'v1.2.10' -> (1, 2, 10). Non-numeric suffixes are ignored."""
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def repo_slug(repo_url: str) -> str:
    """'https://github.com/owner/name(.git)' -> 'owner/name'."""
    m = re.search(r"github\.com[/:]([^/]+/[^/#?]+?)(?:\.git)?/?$", repo_url or "")
    if not m:
        raise ValueError(f"无法识别的仓库地址：{repo_url}")
    return m.group(1)


def changelog_since(changelog: str, current: str) -> str:
    """Return the CHANGELOG sections that are newer than ``current``."""
    cur = parse_version(current)
    out, keep = [], False
    for line in changelog.splitlines():
        m = re.match(r"^##\s*\[?v?(\d+\.\d+\.\d+)", line)
        if m:
            keep = parse_version(m.group(1)) > cur
        if keep:
            out.append(line)
    return "\n".join(out).strip()


async def fetch_text(
    repo: str, path: str, branch: str = "main", mirror: str = DOWNLOAD_MIRROR
) -> str:
    """Fetch a file from the repo, trying each source until one works."""
    sources = [RAW_URL]
    if mirror:
        sources.append(mirror.rstrip("/") + "/" + RAW_URL)
    sources.append(CDN_URL)
    errors = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for tpl in sources:
            url = tpl.format(repo=repo, branch=branch, path=path)
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.text.strip():
                    return resp.text
                errors.append(f"{url.split('/')[2]}: HTTP {resp.status_code}")
            except httpx.HTTPError as e:
                errors.append(f"{url.split('/')[2]}: {type(e).__name__}")
    raise RuntimeError("无法连接 GitHub 检查更新（" + "；".join(errors) + "）")


async def check_latest(mirror: str = DOWNLOAD_MIRROR) -> dict:
    """Compare the local version with the one on GitHub.

    Returns:
        dict with current, latest, has_update, notes (changelog of newer versions).
    """
    local = read_metadata()
    current = local.get("version", "0.0.0")
    repo = repo_slug(local.get("repo", ""))
    remote = read_metadata(await fetch_text(repo, "metadata.yaml", mirror=mirror))
    latest = remote.get("version", "0.0.0")
    has_update = parse_version(latest) > parse_version(current)
    notes = ""
    if has_update:
        try:
            notes = changelog_since(
                await fetch_text(repo, "CHANGELOG.md", mirror=mirror), current
            )
        except RuntimeError:
            notes = ""
    return {
        "current": current,
        "latest": latest,
        "has_update": has_update,
        "notes": notes,
        "astrbot_version": remote.get("astrbot_version", ""),
        "repo": local.get("repo", ""),
    }
