from datetime import datetime

from data.plugins.astrbot_plugin_douyin_ai.core.utils import in_active_hours, parse_json


def test_parse_json_plain():
    assert parse_json('{"comment": true, "text": "哈哈"}') == {
        "comment": True,
        "text": "哈哈",
    }


def test_parse_json_wrapped_in_text_and_code_fence():
    raw = '好的，结果如下：\n```json\n{"reply": false, "reason": "广告"}\n```'
    assert parse_json(raw) == {"reply": False, "reason": "广告"}


def test_parse_json_invalid():
    assert parse_json("我不想评论") == {}
    assert parse_json("{broken") == {}
    assert parse_json("") == {}


def at(hour):
    return datetime(2026, 1, 1, hour)


def test_active_hours_normal_range():
    assert in_active_hours("9-23", at(9))
    assert in_active_hours("9-23", at(22))
    assert not in_active_hours("9-23", at(23))
    assert not in_active_hours("9-23", at(3))


def test_active_hours_overnight_range():
    assert in_active_hours("22-2", at(23))
    assert in_active_hours("22-2", at(1))
    assert not in_active_hours("22-2", at(12))


def test_active_hours_empty_or_invalid_means_always():
    assert in_active_hours("", at(4))
    assert in_active_hours("whenever", at(4))
