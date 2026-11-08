"""Test setup.

The plugin imports ``astrbot.*``, so the tests need an AstrBot checkout:

    ASTRBOT_ROOT=/path/to/AstrBot pytest

The plugin is linked into ``$ASTRBOT_ROOT/data/plugins`` so it can be imported the
same way AstrBot imports it (``data.plugins.astrbot_plugin_douyin_ai``).
"""

import os
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGIN_NAME = "astrbot_plugin_douyin_ai"

_candidates = [os.environ.get("ASTRBOT_ROOT"), PLUGIN_DIR.parent / "AstrBot"]
ASTRBOT_ROOT = next(
    (Path(p).resolve() for p in _candidates if p and (Path(p) / "astrbot").is_dir()),
    None,
)
if ASTRBOT_ROOT is None:
    raise pytest.UsageError(
        "Set ASTRBOT_ROOT to an AstrBot checkout (git clone https://github.com/AstrBotDevs/AstrBot)"
    )

sys.path.insert(0, str(ASTRBOT_ROOT))
_link = ASTRBOT_ROOT / "data" / "plugins" / PLUGIN_NAME
if not _link.exists():
    _link.parent.mkdir(parents=True, exist_ok=True)
    _link.symlink_to(PLUGIN_DIR, target_is_directory=True)
elif _link.resolve() != PLUGIN_DIR:
    raise pytest.UsageError(f"{_link} exists and is not this checkout")

PKG = f"data.plugins.{PLUGIN_NAME}"


@pytest.fixture
def chromium_path():
    """Optional path to a Chromium binary (DOUYIN_TEST_CHROMIUM); '' = playwright default."""
    return os.environ.get("DOUYIN_TEST_CHROMIUM", "")
