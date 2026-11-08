from data.plugins.astrbot_plugin_douyin_ai.core.store import Store


def test_pending_queue_and_persistence(tmp_path):
    s = Store(tmp_path / "state.json")
    a = s.add_pending({"type": "comment", "aweme_id": "v1", "text": "hi"})
    b = s.add_pending({"type": "reply", "cid": "c1", "text": "yo"})
    assert (a, b) == (1, 2)
    assert s.has_pending_for("aweme_id", "v1")
    assert s.has_pending_for("cid", "c1")

    reloaded = Store(tmp_path / "state.json")
    assert set(reloaded.list_pending()) == {"1", "2"}
    assert reloaded.pop_pending(1)["text"] == "hi"
    assert reloaded.pop_pending("1") is None


def test_dedupe_and_daily_counters(tmp_path):
    s = Store(tmp_path / "state.json")
    s.mark_commented("v1")
    s.mark_commented("v1")
    s.mark_handled("c1")
    assert s.has_commented("v1") and not s.has_commented("v2")
    assert s.is_handled("c1")
    assert s.data["commented_awemes"] == ["v1"]

    assert s.count_today("comments") == 0
    s.incr_today("comments")
    s.incr_today("comments")
    assert s.count_today("comments") == 2
    assert Store(tmp_path / "state.json").count_today("comments") == 2


def test_corrupted_file_is_ignored(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json", "utf-8")
    assert Store(p).list_pending() == {}
