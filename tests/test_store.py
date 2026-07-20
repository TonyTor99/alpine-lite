"""Тесты стора: настройки, пауза, счётчики, tg_targets."""
from alpine_lite.store import Store


def _store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def test_settings_and_pause(tmp_path):
    st = _store(tmp_path)
    assert st.is_paused() is False
    st.set_paused(True)
    assert st.is_paused() is True
    st.set_setting("interval", "30")
    assert st.get_int_setting("interval", 10) == 30
    assert st.get_int_setting("missing", 10) == 10
    assert st.get_bool_setting("reports_enabled", True) is True
    st.set_setting("reports_enabled", "0")
    assert st.get_bool_setting("reports_enabled", True) is False
    st.close()


def test_signal_targets_and_counts(tmp_path):
    st = _store(tmp_path)
    sid = st.add_source("Test", "https://alpinbet.com/dispatch/idX/test")
    st.create_signal(sid, "f1", home_team="A", away_team="B", caption_html="cap")
    # tg_targets дописываются постфактум (как делает воркер)
    st.set_signal_tg_targets(sid, "f1", [("-100", 55)])
    row = st.get_signal(sid, "f1")
    assert Store.signal_tg_targets(row) == [("-100", 55)]

    st.mark_settled(sid, "f1", "win", 950)
    sent, win, lose, ret = st.counts_since_source(sid, "2000-01-01T00:00:00+00:00")
    assert (sent, win, lose, ret) == (1, 1, 0, 0)

    recent = st.recent_signals(sid, 5)
    assert len(recent) == 1 and recent[0]["outcome"] == "win"
    st.close()
